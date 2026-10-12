"""Turn zones x keywords x sources into a prioritised queue of search jobs."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass

from leadforge.core.text import norm_place
from leadforge.geography.zones import ensure_allowed
from leadforge.keywords.library import expected_yield, keywords_for_country

CITY_WEIGHT = {"A": 3, "B": 2, "C": 1}
OPEN_STATUSES = ("Not started", "In progress", "Pending")


@dataclass
class PlanResult:
    zones: int = 0
    jobs_created: int = 0
    zones_already_planned: int = 0


def make_query(term: str, area: str, city: str) -> str:
    if norm_place(area) == norm_place(city):
        return f"{term} {city}"
    return f"{term} {area} {city}"


def _ranked_keywords(conn, niche, country):
    kws = keywords_for_country(conn, niche, country)
    scored = [(expected_yield(conn, k, country), k) for k in kws]
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def _queue_round(conn, zone, ranked, sources, run_id, count, base_priority, now) -> int:
    """Add the next `count` unplanned keywords for each source as one new round."""
    created = 0
    rnd = conn.execute("SELECT COALESCE(MAX(round), 0) + 1 FROM search_job WHERE zone_id=?",
                       (zone["zone_id"],)).fetchone()[0]
    for source in sources:
        planned = {r[0] for r in conn.execute(
            "SELECT keyword_id FROM search_job WHERE zone_id=? AND source=?", (zone["zone_id"], source))}
        fresh = [(y, k) for y, k in ranked if k["keyword_id"] not in planned][:count]
        for y, k in fresh:
            cur = conn.execute(
                """INSERT OR IGNORE INTO search_job(run_id, zone_id, keyword_id, source, query, round,
                   priority, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, zone["zone_id"], k["keyword_id"], source,
                 make_query(k["term"], zone["area"], zone["city"]), rnd, round(base_priority + 10 * y, 3), now),
            )
            created += cur.rowcount
    return created


def _register_run(conn, run_id, now, config: dict) -> None:
    conn.execute("INSERT OR IGNORE INTO run(run_id, kind, started_at, config_json) VALUES (?,?,?,?)",
                 (run_id, "plan", now, json.dumps(config)))


def _base_priority(city_priority: str | None, status: str | None) -> int:
    return CITY_WEIGHT.get(city_priority or "C", 1) * 100 + (20 if status == "In progress" else 0)


def plan(conn: sqlite3.Connection, niche: str, countries: list[str], sources: list[str], run_id: str,
         per_zone: int = 4, city: str | None = None, limit_zones: int | None = None) -> PlanResult:
    """Queue round 1 for every open zone that has no jobs yet."""
    res = PlanResult()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for country in countries:
        ensure_allowed(conn, country)
    _register_run(conn, run_id, now, {"niche": niche, "countries": countries, "sources": sources,
                                      "per_zone": per_zone, "city": city})
    for country in countries:
        ranked = _ranked_keywords(conn, niche, country)
        sql = f"""SELECT z.zone_id, z.area, z.city, z.status, g.priority FROM zone z
                  LEFT JOIN geo_place g ON g.place_id = z.place_id
                  WHERE z.country=? AND z.alias_of IS NULL AND z.status IN ({','.join('?' * len(OPEN_STATUSES))})"""
        args: list = [country, *OPEN_STATUSES]
        if city:
            sql += " AND z.city=?"
            args.append(city)
        sql += " ORDER BY COALESCE(g.priority, 'C'), z.zone_id"
        if limit_zones:
            sql += f" LIMIT {int(limit_zones)}"
        for z in conn.execute(sql, args).fetchall():
            res.zones += 1
            if conn.execute("SELECT 1 FROM search_job WHERE zone_id=? LIMIT 1", (z["zone_id"],)).fetchone():
                res.zones_already_planned += 1
                continue
            res.jobs_created += _queue_round(conn, z, ranked, sources, run_id, per_zone,
                                             _base_priority(z["priority"], z["status"]), now)
    return res


def extend_round(conn: sqlite3.Connection, zone_id: int, niche: str, sources: list[str], run_id: str,
                 per_round: int = 4) -> int:
    """Queue the next round of keywords for one zone, once its previous round is finished."""
    z = conn.execute(
        """SELECT z.zone_id, z.area, z.city, z.country, z.status, g.priority FROM zone z
           LEFT JOIN geo_place g ON g.place_id = z.place_id WHERE z.zone_id=?""", (zone_id,)).fetchone()
    ensure_allowed(conn, z["country"])
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _register_run(conn, run_id, now, {"niche": niche, "zone_id": zone_id, "sources": sources})
    return _queue_round(conn, z, _ranked_keywords(conn, niche, z["country"]), sources, run_id, per_round,
                        _base_priority(z["priority"], z["status"]), now)
