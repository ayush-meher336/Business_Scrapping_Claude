"""Zone coverage: what each zone has produced, and whether it can be called complete."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from leadforge.core.text import norm_place

MIN_CORE_KEYWORDS = 3
LOW_YIELD_RATE = 0.05


def relink_leads(conn: sqlite3.Connection) -> int:
    """Link leads without a zone to one by normalised country + city + suburb name."""
    index: dict[tuple[str, str, str], int] = {}
    for z in conn.execute("SELECT zone_id, country, city, area, alias_of FROM zone ORDER BY zone_id"):
        key = (norm_place(z["country"]), norm_place(z["city"]), norm_place(z["area"]))
        index.setdefault(key, z["alias_of"] or z["zone_id"])
    linked = 0
    for b in conn.execute("SELECT lead_key, country, city, suburb_area FROM business WHERE zone_id IS NULL").fetchall():
        zid = index.get((norm_place(b["country"]), norm_place(b["city"]), norm_place(b["suburb_area"])))
        if zid:
            conn.execute("UPDATE business SET zone_id=? WHERE lead_key=?", (zid, b["lead_key"]))
            linked += 1
    return linked


def city_coverage(conn: sqlite3.Connection, country: str | None = None) -> list[sqlite3.Row]:
    sql = """
    SELECT g.country, g.name AS city, g.size_code, g.priority, g.status AS city_status,
           COUNT(DISTINCT z.zone_id) AS zones,
           COUNT(DISTINCT CASE WHEN z.status='Done' THEN z.zone_id END) AS zones_done,
           COUNT(DISTINCT CASE WHEN z.source != 'legacy_workbook' THEN z.zone_id END) AS zones_new,
           (SELECT COUNT(*) FROM business b WHERE b.country=g.country AND b.city=g.name) AS leads,
           (SELECT COUNT(*) FROM business b WHERE b.country=g.country AND b.city=g.name AND b.website IS NOT NULL) AS with_website,
           (SELECT COUNT(*) FROM business b WHERE b.country=g.country AND b.city=g.name AND b.phone IS NOT NULL) AS with_phone,
           (SELECT COUNT(*) FROM business b WHERE b.country=g.country AND b.city=g.name
                AND (b.general_email IS NOT NULL OR b.decision_maker_email IS NOT NULL)) AS with_email
    FROM geo_place g LEFT JOIN zone z ON z.place_id = g.place_id AND z.alias_of IS NULL
    WHERE g.level='city' {where}
    GROUP BY g.place_id
    ORDER BY g.country, COALESCE(g.priority,'C'), g.name"""
    if country:
        return conn.execute(sql.format(where="AND g.country=?"), (country,)).fetchall()
    return conn.execute(sql.format(where="")).fetchall()


@dataclass
class ZoneVerdict:
    status: str          # unchanged | In progress | Done | Exhausted | Blocked
    reason: str


def evaluate_zone(conn: sqlite3.Connection, zone_id: int, enabled_sources: list[str]) -> ZoneVerdict:
    """Apply the completion rule. One empty search is never enough to close a zone."""
    jobs = conn.execute(
        """SELECT j.round, j.source, j.status, j.results, j.new_uniques, k.kind
           FROM search_job j JOIN keyword k ON k.keyword_id = j.keyword_id WHERE j.zone_id=?""", (zone_id,)
    ).fetchall()
    if not jobs:
        return ZoneVerdict("unchanged", "no search jobs yet")
    if any(j["status"] in ("pending", "claimed") for j in jobs):
        if any(j["status"] == "done" for j in jobs):
            return ZoneVerdict("In progress", "jobs still queued")
        return ZoneVerdict("unchanged", "jobs queued, none run")
    done = [j for j in jobs if j["status"] == "done"]
    if not done and any(j["status"] == "blocked" for j in jobs):
        return ZoneVerdict("Blocked", "every job was blocked (needs human review)")

    rounds: dict[int, list] = {}
    for j in done:
        rounds.setdefault(j["round"], []).append(j)
    order = sorted(rounds)
    new_by_round = [sum(j["new_uniques"] or 0 for j in rounds[r]) for r in order]
    if len(order) >= 2 and new_by_round[-1] == 0 and new_by_round[-2] == 0:
        return ZoneVerdict("Exhausted", "two consecutive rounds found no new businesses")

    core_done = sum(1 for j in done if j["kind"] in ("core", "local"))
    sources_done = {j["source"] for j in done}
    last = rounds[order[-1]]
    results = sum(j["results"] or 0 for j in last)
    rate = (new_by_round[-1] / results) if results else 0.0
    missing = [s for s in enabled_sources if s not in sources_done]
    if core_done < MIN_CORE_KEYWORDS:
        return ZoneVerdict("In progress", f"{core_done}/{MIN_CORE_KEYWORDS} core keywords run")
    if missing:
        return ZoneVerdict("In progress", f"sources not yet queried: {', '.join(missing)}")
    if rate >= LOW_YIELD_RATE:
        return ZoneVerdict("In progress", f"last round still {rate:.0%} new; queue another round")
    return ZoneVerdict("Done", f"{core_done} core keywords, all sources, last round {rate:.0%} new")
