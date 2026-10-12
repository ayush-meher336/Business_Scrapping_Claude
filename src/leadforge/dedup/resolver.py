"""Resolve a discovery candidate to a business: new record, merge, review, or reject.

Match rules, evaluated in order (architecture doc, Deduplication):
  1. same Maps place id                                    -> merge
  2. same domain and same street address / within 150 m    -> merge
  2b. same street address plus name >= 90, same phone, same domain, or name in address -> merge
  2c. same domain, one listing has no street address, similar name -> merge
  3. same E.164 phone (not shared) and name similarity >= 85 -> merge
  4. same domain, more than 1 km apart                     -> separate branch (group_domain)
  5. name similarity >= 90 in the same city, no conflicting phone / domain -> new record + review
Fuzzy similarity alone never merges.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz

from leadforge.core.ids import new_ulid
from leadforge.core.text import bbox, haversine_km, norm_place, strip_accents
from leadforge.dedup.normalize import clean_website, name_norm, phone_e164, registrable_domain

SAME_SITE_KM = 0.15
ADDRESS_NAME_MIN = 90
ZONE_SNAP_KM = 5.0       # a business belongs to the nearest zone within this distance
LOCALITY_SNAP_KM = 3.0   # otherwise to the nearest named suburb / town within this distance
BRANCH_KM = 1.0
PHONE_NAME_MIN = 85
REVIEW_NAME_MIN = 90
SHARED_PHONE_MIN_USERS = 3


@dataclass
class Resolution:
    action: str                 # new | merged | review | rejected
    lead_key: str | None
    reason: str
    candidate_id: int | None = None


@dataclass
class Context:
    """Where the candidate was found."""
    zone_id: int | None
    country: str
    iso2: str | None
    region: str | None
    state: str | None
    city: str
    area: str
    zone_lat: float | None
    zone_lon: float | None
    zone_radius_m: int | None
    keyword: str | None
    run_id: str
    job_id: int | None


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


_ADDR = re.compile(r"(\d+[a-z]?(?:\s*[/-]\s*\d+[a-z]?)*)\s+([a-z][a-z' .]+)")


def street_parts(address: str | None) -> tuple[frozenset, str] | None:
    """'Unit 19/23-25 Oxford St' -> ({'19', '23', '25'}, 'oxford st').

    Text before the street (a facility name, 'Weekend Transfer Station, ...') is ignored.
    """
    if not address:
        return None
    a = strip_accents(address).casefold()
    m = None
    for m in _ADDR.finditer(a):
        pass                                     # the last number + street wins
    if not m:
        return None
    nums = frozenset(re.split(r"\s*[/-]\s*", m.group(1)))
    street = norm_place(m.group(2))
    return (nums, street) if street else None


def same_address(a: str | None, b: str | None) -> bool:
    pa, pb = street_parts(a), street_parts(b)
    return bool(pa and pb and pa[1] == pb[1] and pa[0] & pb[0])


def _is_shared_phone(conn, e164: str, name: str) -> bool:
    rows = conn.execute("SELECT name_norm FROM business WHERE phone_e164=?", (e164,)).fetchall()
    others = {r[0] for r in rows if r[0] and fuzz.token_set_ratio(r[0], name) < PHONE_NAME_MIN}
    return len(others) + 1 >= SHARED_PHONE_MIN_USERS


def find_match(conn: sqlite3.Connection, cand: dict, ctx: Context) -> tuple[str, sqlite3.Row | None, str, float]:
    """(verdict, matched business, rule, score). verdict: merge | branch | review | none."""
    nn = cand["name_norm"]
    if cand.get("place_id"):
        b = conn.execute("SELECT * FROM business WHERE google_place_id=?", (cand["place_id"],)).fetchone()
        if b:
            return "merge", b, "1:place_id", 100.0

    domain = cand.get("domain")
    if domain:
        for b in conn.execute("SELECT * FROM business WHERE domain=?", (domain,)).fetchall():
            if cand.get("lat") is not None and b["lat"] is not None:
                d = haversine_km(cand["lat"], cand["lon"], b["lat"], b["lon"])
                if d <= SAME_SITE_KM:
                    return "merge", b, "2:domain+location", 100.0
                if d > BRANCH_KM:
                    continue
            if same_address(cand.get("address"), b["street_address"]):
                return "merge", b, "2:domain+address", 100.0
            if b["lat"] is None and norm_place(b["suburb_area"]) == norm_place(ctx.area) and \
                    fuzz.token_set_ratio(nn, b["name_norm"] or "") >= PHONE_NAME_MIN:
                # Legacy rows have no coordinates: same website + suburb + name is the same site.
                return "merge", b, "2:domain+suburb+name", 95.0

    if street_parts(cand.get("address")):
        # Same premises: the address alone is not enough (shared buildings), but together with a
        # near-identical name, the same phone, the same website, or one name inside the other's address.
        for b in conn.execute("SELECT * FROM business WHERE country=? AND city=? AND street_address IS NOT NULL",
                              (ctx.country, ctx.city)).fetchall():
            if not same_address(cand["address"], b["street_address"]):
                continue
            score = fuzz.token_set_ratio(nn, b["name_norm"] or "")
            if score >= ADDRESS_NAME_MIN:
                return "merge", b, "2b:address+name", float(score)
            if cand.get("phone_e164") and cand["phone_e164"] == b["phone_e164"]:
                return "merge", b, "2b:address+phone", float(score)
            if domain and domain == b["domain"]:
                return "merge", b, "2b:address+domain", float(score)
            if (b["name_norm"] and b["name_norm"] in norm_place(cand["address"])) or                     (nn and nn in norm_place(b["street_address"])):
                return "merge", b, "2b:address+name-in-address", float(score)

    e164 = cand.get("phone_e164")
    if e164 and not _is_shared_phone(conn, e164, nn):
        for b in conn.execute("SELECT * FROM business WHERE phone_e164=?", (e164,)).fetchall():
            score = fuzz.token_set_ratio(nn, b["name_norm"] or "")
            if score >= PHONE_NAME_MIN:
                return "merge", b, "3:phone+name", float(score)

    if domain:
        rows = conn.execute("SELECT * FROM business WHERE domain=?", (domain,)).fetchall()
        for b in rows:
            # A service-area listing (no street) of a business we already have under the same website.
            if (not cand.get("address") or not b["street_address"]) and                     fuzz.token_set_ratio(nn, b["name_norm"] or "") >= PHONE_NAME_MIN:
                return "merge", b, "2c:domain+no-address", 100.0
        if rows:
            return "branch", rows[0], "4:domain-other-site", 100.0

    best, best_score = None, 0.0
    for b in conn.execute("SELECT * FROM business WHERE country=? AND city=? AND name_norm IS NOT NULL",
                          (ctx.country, ctx.city)).fetchall():
        score = fuzz.token_set_ratio(nn, b["name_norm"])
        if score > best_score:
            best, best_score = b, score
    if best is not None and best_score >= REVIEW_NAME_MIN:
        conflict = (e164 and best["phone_e164"] and e164 != best["phone_e164"]) or \
                   (domain and best["domain"] and domain != best["domain"])
        if not conflict:
            return "review", best, "5:similar-name", best_score
    return "none", None, "", 0.0


def locate(conn: sqlite3.Connection, cand: dict, ctx: Context) -> tuple[int | None, str | None, str | None]:
    """(zone_id, suburb, located-in-zone) from the business's own coordinates.

    A Maps search for one suburb returns businesses from across the metro area, so the zone that
    ran the search says little about where a business is.
    """
    lat, lon = cand.get("lat"), cand.get("lon")
    if lat is None:
        return ctx.zone_id, ctx.area, None
    lo_lat, hi_lat, lo_lon, hi_lon = bbox(lat, lon, ZONE_SNAP_KM)
    zones = conn.execute(
        """SELECT zone_id, area, lat, lon, radius_m FROM zone WHERE country=? AND alias_of IS NULL AND lat
           BETWEEN ? AND ? AND lon BETWEEN ? AND ?""", (ctx.country, lo_lat, hi_lat, lo_lon, hi_lon)).fetchall()
    near = sorted(((haversine_km(lat, lon, z["lat"], z["lon"]), z) for z in zones), key=lambda t: t[0])
    if near:
        d, z = near[0]
        inside = d * 1000 <= (z["radius_m"] or 2000)
        return z["zone_id"], z["area"], "Yes - premises in this suburb" if inside else "Nearby suburb"
    lo_lat, hi_lat, lo_lon, hi_lon = bbox(lat, lon, LOCALITY_SNAP_KM)
    places = conn.execute(
        """SELECT name, lat, lon FROM gn_place WHERE country_code=? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
           UNION ALL SELECT name, lat, lon FROM osm_feature WHERE kind IN ('suburb','neighbourhood','quarter')
           AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
        (ctx.iso2, lo_lat, hi_lat, lo_lon, hi_lon, lo_lat, hi_lat, lo_lon, hi_lon)).fetchall()
    if places:
        p = min(places, key=lambda r: haversine_km(lat, lon, r["lat"], r["lon"]))
        if haversine_km(lat, lon, p["lat"], p["lon"]) <= LOCALITY_SNAP_KM:
            return None, p["name"], "No - service-area only (based elsewhere)"
    return None, None, None


def _insert_business(conn, cand: dict, ctx: Context, group_domain: str | None) -> str:
    lead_key = new_ulid()
    lead_id = conn.execute("SELECT COALESCE(MAX(lead_id), 0) + 1 FROM business").fetchone()[0]
    zone_id, suburb, located = locate(conn, cand, ctx)
    ts = now()
    conn.execute(
        """INSERT INTO business(lead_key, lead_id, region, country, state_province, city, suburb_area, company_name,
           website, located_in_zone, street_address, phone, source_url, source_type, found_via_keyword,
           date_collected, collected_by, google_maps_url, google_place_id, lat, lon, zone_id, research_status,
           first_discovered_at, last_updated_at, domain, phone_e164, name_norm, group_domain, maps_category,
           maps_rating, permanently_closed, email_verified, outreach_status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (lead_key, lead_id, ctx.region, ctx.country, ctx.state, ctx.city, suburb, cand["name"],
         cand.get("website"), located, cand.get("address"), cand.get("phone"),
         cand.get("maps_url") or cand.get("website"), cand["source_type"], ctx.keyword, ts[:10], "leadforge",
         cand.get("maps_url"), cand.get("place_id"), cand.get("lat"), cand.get("lon"), zone_id,
         "discovered", ts, ts, cand.get("domain"), cand.get("phone_e164"), cand["name_norm"], group_domain,
         cand.get("category"), cand.get("rating"), int(bool(cand.get("permanently_closed"))),
         "Not checked", "Not contacted"),
    )
    return lead_key


def _add_evidence(conn, lead_key: str, cand: dict, ctx: Context) -> None:
    conn.execute(
        """INSERT INTO source_record(lead_key, source_category, source_name, source_url, evidence_type,
           fields_supported, date_accessed, verification_status, run_id, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (lead_key, cand["source_type"], cand["source"], cand.get("maps_url") or cand.get("website"),
         "discovery_listing", ",".join(k for k in ("name", "category", "address", "phone", "website")
                                       if cand.get(k)), now()[:10], "listed", ctx.run_id,
         f"query: {ctx.keyword}" if ctx.keyword else None),
    )
    if cand.get("phone"):
        conn.execute("""INSERT OR IGNORE INTO contact(lead_key, kind, value, role) VALUES (?,?,?,?)""",
                     (lead_key, "phone", cand["phone"], "general"))


def _fill_gaps(conn, b: sqlite3.Row, cand: dict) -> None:
    """A merge only fills empty research fields; it never overwrites what is already there."""
    updates = {
        "website": cand.get("website"), "domain": cand.get("domain"), "phone": cand.get("phone"),
        "phone_e164": cand.get("phone_e164"), "street_address": cand.get("address"),
        "google_place_id": cand.get("place_id"), "google_maps_url": cand.get("maps_url"),
        "lat": cand.get("lat"), "lon": cand.get("lon"), "maps_category": cand.get("category"),
        "maps_rating": cand.get("rating"), "name_norm": cand["name_norm"],
    }
    sets = {k: v for k, v in updates.items() if v is not None and b[k] in (None, "")}
    if cand.get("permanently_closed") and not b["permanently_closed"]:
        sets["permanently_closed"] = 1
    if sets:
        sets["last_updated_at"] = now()
        conn.execute(f"UPDATE business SET {', '.join(f'{k}=?' for k in sets)} WHERE lead_key=?",
                     (*sets.values(), b["lead_key"]))


def prepare(raw: dict, iso2: str | None) -> dict:
    cand = dict(raw)
    cand["website"] = clean_website(cand.get("website"))
    cand["domain"] = registrable_domain(cand.get("website"))
    cand["phone_e164"] = phone_e164(cand.get("phone"), iso2)
    cand["name_norm"] = name_norm(cand.get("name"))
    return cand


def record_candidate(conn, cand: dict, ctx: Context) -> int:
    cur = conn.execute(
        """INSERT INTO candidate(job_id, source, source_ref, name, category, address, phone, website, lat, lon,
           rating, reviews, permanently_closed, maps_url, snippet, raw_json, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ctx.job_id, cand["source"], cand.get("place_id") or cand.get("website"), cand.get("name"),
         cand.get("category"), cand.get("address"), cand.get("phone"), cand.get("website"), cand.get("lat"),
         cand.get("lon"), cand.get("rating"), cand.get("reviews"), int(bool(cand.get("permanently_closed"))),
         cand.get("maps_url"), cand.get("snippet"), json.dumps(cand.get("raw") or {}, ensure_ascii=False), now()),
    )
    return cur.lastrowid


def resolve(conn: sqlite3.Connection, raw: dict, ctx: Context, relevant: bool = True,
            reject_reason: str = "") -> Resolution:
    """Store the candidate and decide what it is. Call inside a transaction."""
    cand = prepare(raw, ctx.iso2)
    cid = record_candidate(conn, cand, ctx)

    def done(action, lead_key, reason):
        conn.execute("UPDATE candidate SET lead_key=?, resolution=?, reason=? WHERE candidate_id=?",
                     (lead_key, action, reason, cid))
        return Resolution(action, lead_key, reason, cid)

    if not cand["name_norm"]:
        return done("rejected", None, "no name")
    if not relevant:
        return done("rejected", None, reject_reason or "not relevant to niche")

    verdict, b, rule, score = find_match(conn, cand, ctx)
    if verdict == "merge":
        _fill_gaps(conn, b, cand)
        _add_evidence(conn, b["lead_key"], cand, ctx)
        conn.execute("INSERT INTO merge_log(lead_key, candidate_id, rule, score, created_at) VALUES (?,?,?,?,?)",
                     (b["lead_key"], cid, rule, score, now()))
        return done("merged", b["lead_key"], rule)

    group = cand.get("domain") if verdict == "branch" else None
    lead_key = _insert_business(conn, cand, ctx, group)
    _add_evidence(conn, lead_key, cand, ctx)
    if verdict == "branch":
        conn.execute("UPDATE business SET group_domain=? WHERE lead_key=?", (group, b["lead_key"]))
        return done("new", lead_key, rule)
    if verdict == "review":
        conn.execute("""INSERT INTO dedup_review(candidate_id, lead_key, match_key, score, reason, created_at)
                        VALUES (?,?,?,?,?,?)""", (cid, lead_key, b["lead_key"], score, rule, now()))
        return done("review", lead_key, f"{rule} ({score:.0f}) with lead {b['lead_id']}")
    return done("new", lead_key, "no match")


def backfill_identity(conn: sqlite3.Connection) -> dict[str, int]:
    """Compute domain / E.164 / normalised name for businesses that lack them (e.g. legacy rows)."""
    iso = {r[0]: r[1] for r in conn.execute("SELECT name, iso2 FROM country")}
    stats = {"domain": 0, "phone": 0, "name": 0}
    rows = conn.execute("""SELECT lead_key, company_name, website, phone, country FROM business
                           WHERE name_norm IS NULL OR (domain IS NULL AND website IS NOT NULL)
                           OR (phone_e164 IS NULL AND phone IS NOT NULL)""").fetchall()
    for r in rows:
        d = registrable_domain(r["website"])
        p = phone_e164(r["phone"], iso.get(r["country"]))
        n = name_norm(r["company_name"])
        conn.execute("""UPDATE business SET domain=COALESCE(domain, ?), phone_e164=COALESCE(phone_e164, ?),
                        name_norm=COALESCE(name_norm, ?) WHERE lead_key=?""", (d, p, n, r["lead_key"]))
        stats["domain"] += d is not None
        stats["phone"] += p is not None
        stats["name"] += bool(n)
    return stats
