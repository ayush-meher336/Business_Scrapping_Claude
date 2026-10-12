"""Attach ISO codes and coordinates to the workbook's countries, cities and zones."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from leadforge.core.text import haversine_km, norm_place

ZONE_MAX_KM = 80  # a zone's GeoNames match must lie this close to its city


def name_variants(name: str) -> list[str]:
    """'Kandla / Gandhidham (port)' -> ['kandla gandhidham port', 'kandla', 'gandhidham'] etc."""
    variants = [name]
    no_paren = re.sub(r"\(.*?\)", " ", name).strip()
    variants.append(no_paren)
    for part in re.split(r"\s*(?:/|,|&|\band\b|-\s)\s*", no_paren):
        variants.append(part)
    seen, out = set(), []
    for v in variants:
        k = norm_place(v)
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _initials(s: str) -> str:
    return "".join(w[0] for w in norm_place(s).split() if w)


def admin_matches(state: str | None, admin_name: str | None) -> bool:
    if not state or not admin_name:
        return False
    s, a = norm_place(state), norm_place(admin_name)
    return s == a or s == _initials(admin_name) or (len(s) >= 3 and a.startswith(s))


# -- countries -------------------------------------------------------------
def resolve_countries(conn: sqlite3.Connection, aliases: dict[str, str],
                      excluded: list[str], reason: str) -> list[str]:
    by_name = {norm_place(r["name"]): r["iso2"] for r in conn.execute("SELECT iso2, name FROM gn_country")}
    alias = {norm_place(k): v for k, v in aliases.items()}
    excluded_norm = {norm_place(n) for n in excluded}
    unmatched = []
    for row in conn.execute("SELECT name FROM country").fetchall():
        key = norm_place(row["name"])
        iso2 = alias.get(key) or by_name.get(key)
        if iso2 is None:
            unmatched.append(row["name"])
        is_excl = key in excluded_norm
        conn.execute(
            "UPDATE country SET iso2=?, excluded=?, excluded_reason=? WHERE name=?",
            (iso2, int(is_excl), reason if is_excl else None, row["name"]),
        )
    return unmatched


def iso_for(conn: sqlite3.Connection, country: str) -> str | None:
    row = conn.execute("SELECT iso2 FROM country WHERE name=?", (country,)).fetchone()
    return row["iso2"] if row else None


# -- candidates ------------------------------------------------------------
@dataclass
class Candidate:
    geonameid: int
    name: str
    lat: float
    lon: float
    feature_code: str
    population: int
    admin1_name: str | None


def candidates(conn: sqlite3.Connection, iso2: str, name_norm: str) -> list[Candidate]:
    rows = conn.execute(
        """SELECT p.geonameid, p.name, p.lat, p.lon, p.feature_code, p.population, a.name AS admin1_name
           FROM gn_name n JOIN gn_place p ON p.geonameid = n.geonameid
           LEFT JOIN gn_admin1 a ON a.code = p.country_code || '.' || p.admin1_code
           WHERE n.name_norm = ? AND p.country_code = ?""",
        (name_norm, iso2),
    ).fetchall()
    return [Candidate(**dict(r)) for r in rows]


_FEATURE_RANK = {"PPLC": 4, "PPLA": 3, "PPLA2": 2, "PPLA3": 1, "PPL": 1}


def pick_city(cands: list[Candidate], state: str | None) -> tuple[Candidate | None, str]:
    if not cands:
        return None, "unmatched"
    in_state = [c for c in cands if admin_matches(state, c.admin1_name)]
    pool = in_state or cands
    pool.sort(key=lambda c: (c.population, _FEATURE_RANK.get(c.feature_code, 0)), reverse=True)
    best = pool[0]
    if len(pool) > 1 and not in_state and pool[1].population * 3 > best.population:
        return best, "ambiguous"   # two similar-sized same-name places, no state to separate them
    return best, "matched"


# -- cities and zones ------------------------------------------------------
def geocode_cities(conn: sqlite3.Connection, only_missing: bool = True) -> dict[str, int]:
    stats = {"matched": 0, "ambiguous": 0, "unmatched": 0, "no_iso": 0}
    sql = "SELECT place_id, name, country, state_province FROM geo_place WHERE level='city'"
    if only_missing:
        sql += " AND lat IS NULL"
    for row in conn.execute(sql).fetchall():
        iso2 = iso_for(conn, row["country"])
        if not iso2:
            stats["no_iso"] += 1
            continue
        best, status = None, "unmatched"
        for variant in name_variants(row["name"]):
            best, status = pick_city(candidates(conn, iso2, variant), row["state_province"])
            if best:
                break
        stats[status] += 1
        if best:
            conn.execute(
                "UPDATE geo_place SET lat=?, lon=?, source_ref=?, last_reviewed=date('now') WHERE place_id=?",
                (best.lat, best.lon, f"geonames:{best.geonameid}", row["place_id"]),
            )
    return stats


def _osm_match(conn, place_id, variants, clat, clon):
    """Nearest OSM suburb / industrial feature of the same city whose name matches."""
    rows = conn.execute("SELECT osm_ref, name, lat, lon FROM osm_feature WHERE place_id=? AND name IS NOT NULL",
                        (place_id,)).fetchall()
    for v in variants:
        hits = [r for r in rows if norm_place(r["name"]) == v or norm_place(r["name"]).startswith(v + " ")]
        hits = [(haversine_km(clat, clon, r["lat"], r["lon"]), r) for r in hits]
        hits = [(d, r) for d, r in hits if d <= ZONE_MAX_KM]
        if hits:
            return min(hits, key=lambda t: t[0])[1]
    return None


def geocode_zones(conn: sqlite3.Connection, only_missing: bool = True) -> dict[str, int]:
    """GeoNames first; stored area features of the same city as the fallback."""
    stats = {"matched": 0, "matched_osm": 0, "unmatched": 0, "no_city_coords": 0}
    sql = """SELECT z.zone_id, z.area, z.country, z.place_id, g.lat AS clat, g.lon AS clon
             FROM zone z LEFT JOIN geo_place g ON g.place_id = z.place_id WHERE z.alias_of IS NULL"""
    if only_missing:
        sql += " AND z.lat IS NULL"
    for row in conn.execute(sql).fetchall():
        if row["clat"] is None:
            stats["no_city_coords"] += 1
            continue
        iso2 = iso_for(conn, row["country"])
        best = None
        for variant in name_variants(row["area"]):
            near = [
                (haversine_km(row["clat"], row["clon"], c.lat, c.lon), c)
                for c in candidates(conn, iso2, variant)
            ]
            near = [(d, c) for d, c in near if d <= ZONE_MAX_KM]
            if near:
                best = min(near, key=lambda t: t[0])[1]
                break
        if best:
            conn.execute(
                "UPDATE zone SET lat=?, lon=?, source_ref=COALESCE(source_ref, ?), geocode_status='matched' WHERE zone_id=?",
                (best.lat, best.lon, f"geonames:{best.geonameid}", row["zone_id"]),
            )
            stats["matched"] += 1
        elif (f := _osm_match(conn, row["place_id"], name_variants(row["area"]), row["clat"], row["clon"])):
            conn.execute(
                "UPDATE zone SET lat=?, lon=?, source_ref=COALESCE(source_ref, ?), geocode_status='matched' WHERE zone_id=?",
                (f["lat"], f["lon"], f"osm:{f['osm_ref']}", row["zone_id"]),
            )
            stats["matched_osm"] += 1
        else:
            conn.execute("UPDATE zone SET geocode_status='unmatched' WHERE zone_id=?", (row["zone_id"],))
            stats["unmatched"] += 1
    return stats
