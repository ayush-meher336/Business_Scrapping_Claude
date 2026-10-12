"""Expand each city into research zones, keeping every existing zone and its history."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from leadforge.core.ids import new_ulid
from leadforge.core.text import bbox, haversine_km, norm_place


@dataclass
class SizeRule:
    radius_km: float
    mode: str              # suburbs | towns
    min_population: int


@dataclass
class BuildSettings:
    rules: dict[str, SizeRule]
    alias_km: float = 1.5
    surrounding_pop: int = 100000
    surrounding_km: float = 60
    industrial_km: float = 1.5
    min_industrial_sites: int = 3
    estate_pattern: str = r"(?i)\b(industrial|estate|business park|precinct)\b"
    estate_exclude: str = r"(?i)\b(substation|reservoir|water|sewage)\b"

    def is_estate(self, name: str) -> bool:
        return bool(re.search(self.estate_pattern, name)) and not re.search(self.estate_exclude, name)


@dataclass
class BuildResult:
    city: str
    country: str
    existing: int = 0
    existing_geocoded: int = 0
    candidates: int = 0
    matched_existing: int = 0         # candidates that are an existing zone (by name or distance)
    new_zones: list[str] = field(default_factory=list)
    new_cities: list[str] = field(default_factory=list)
    filtered_no_industry: int = 0     # suburbs dropped: no industrial land nearby
    skipped: str | None = None


class ExcludedCountryError(ValueError):
    pass


def ensure_allowed(conn: sqlite3.Connection, country: str) -> None:
    row = conn.execute("SELECT excluded, excluded_reason FROM country WHERE name=?", (country,)).fetchone()
    if row and row["excluded"]:
        raise ExcludedCountryError(f"{country} is excluded: {row['excluded_reason']}")


def _places_near(conn, iso2, lat, lon, km, min_pop):
    lo_lat, hi_lat, lo_lon, hi_lon = bbox(lat, lon, km)
    rows = conn.execute(
        """SELECT geonameid, name, lat, lon, population, feature_code FROM gn_place
           WHERE country_code=? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND population >= ?""",
        (iso2, lo_lat, hi_lat, lo_lon, hi_lon, min_pop),
    ).fetchall()
    out = []
    for r in rows:
        d = haversine_km(lat, lon, r["lat"], r["lon"])
        if d <= km:
            out.append((d, r))
    out.sort(key=lambda t: t[0])
    return out


def _osm_near(conn, place_id):
    return conn.execute(
        "SELECT osm_ref, kind, name, lat, lon FROM osm_feature WHERE place_id=? AND name IS NOT NULL", (place_id,)
    ).fetchall()


def build_city(conn: sqlite3.Connection, place_id: str, cfg: BuildSettings, run_id: str,
               dry_run: bool = False) -> BuildResult:
    city = conn.execute(
        """SELECT g.*, c.iso2 FROM geo_place g JOIN country c ON c.name = g.country
           WHERE g.place_id=?""", (place_id,)).fetchone()
    res = BuildResult(city["name"], city["country"])
    ensure_allowed(conn, city["country"])
    if city["lat"] is None or city["iso2"] is None:
        res.skipped = "city not geocoded"
        return res
    rule = cfg.rules.get(city["size_code"] or "S", cfg.rules["S"])
    alias_km = cfg.alias_km

    existing = conn.execute(
        "SELECT zone_id, area, lat, lon FROM zone WHERE place_id=? AND alias_of IS NULL", (place_id,)
    ).fetchall()
    res.existing = len(existing)
    res.existing_geocoded = sum(1 for z in existing if z["lat"] is not None)
    by_name = {norm_place(z["area"]): z["zone_id"] for z in existing}
    points = [(z["lat"], z["lon"]) for z in existing if z["lat"] is not None]
    city_key = norm_place(city["name"])

    def is_existing(name: str, lat: float, lon: float) -> bool:
        if norm_place(name) in by_name:
            return True
        return any(haversine_km(lat, lon, a, b) <= alias_km for a, b in points)

    osm = _osm_near(conn, place_id)
    if rule.mode == "suburbs" and not osm and not existing:
        # No industrial evidence at all: wait for the Phase 3 Google Maps industrial-area scan.
        res.skipped = "no industrial evidence yet (Phase 3 Maps scan)"
        return res
    # Strong evidence: an estate / precinct, or an existing zone (those were chosen for industry).
    # Weak evidence: single named sites; utilities (substations, water plants) are not counted at all.
    strong = [(f["lat"], f["lon"]) for f in osm if f["kind"] in ("industrial", "port") and cfg.is_estate(f["name"])]
    strong += [(z["lat"], z["lon"]) for z in existing if z["lat"] is not None]
    sites = [(f["lat"], f["lon"]) for f in osm
             if f["kind"] in ("industrial", "port") and not re.search(cfg.estate_exclude, f["name"])]

    def has_industry(lat: float, lon: float) -> bool:
        if any(haversine_km(lat, lon, a, b) <= cfg.industrial_km for a, b in strong):
            return True
        near = sum(1 for a, b in sites if haversine_km(lat, lon, a, b) <= cfg.industrial_km)
        return near >= cfg.min_industrial_sites

    proposals: list[tuple[str, str, str, float, float, int]] = []  # area, kind, source_ref, lat, lon, radius_m
    near = _places_near(conn, city["iso2"], city["lat"], city["lon"], rule.radius_km, rule.min_population)
    for d, p in near:
        if norm_place(p["name"]) == city_key and d < 5:
            continue                                   # the city itself
        res.candidates += 1
        if is_existing(p["name"], p["lat"], p["lon"]):
            res.matched_existing += 1
            continue
        if rule.mode == "suburbs" and not has_industry(p["lat"], p["lon"]):
            res.filtered_no_industry += 1
            continue
        kind = "town" if rule.mode == "towns" else "suburb"
        proposals.append((p["name"], kind, f"geonames:{p['geonameid']}", p["lat"], p["lon"],
                          5000 if kind == "town" else 2000))
        points.append((p["lat"], p["lon"]))
        by_name[norm_place(p["name"])] = -1

    for f in osm:
        if f["kind"] in ("neighbourhood", "quarter"):
            continue                                   # used for geocoding only; too small to be a zone
        if f["kind"] in ("industrial", "port") and not cfg.is_estate(f["name"]):
            continue                                   # a single site: evidence of industry, not a zone
        if f["kind"] == "suburb" and rule.mode == "towns":
            continue
        res.candidates += 1
        if is_existing(f["name"], f["lat"], f["lon"]):
            res.matched_existing += 1
            continue
        if f["kind"] == "suburb" and not has_industry(f["lat"], f["lon"]):
            res.filtered_no_industry += 1
            continue
        proposals.append((f["name"], "suburb" if f["kind"] == "suburb" else "industrial",
                          f"osm:{f['osm_ref']}", f["lat"], f["lon"], 1500))
        points.append((f["lat"], f["lon"]))
        by_name[norm_place(f["name"])] = -1

    if rule.mode == "towns" and not existing:
        # A secondary city is researched as a whole, plus its nearby towns.
        proposals.insert(0, (city["name"], "city", city["source_ref"] or "", city["lat"], city["lon"], 10000))

    res.new_zones = [p[0] for p in proposals]
    res.new_cities = _surrounding_cities(conn, city, cfg.surrounding_pop, cfg.surrounding_km, rule.radius_km,
                                         run_id, dry_run)
    if dry_run:
        return res

    next_id = conn.execute("SELECT COALESCE(MAX(zone_id), 0) + 1 FROM zone").fetchone()[0]
    for i, (area, kind, ref, lat, lon, radius) in enumerate(proposals):
        conn.execute(
            """INSERT INTO zone(zone_id, place_id, country, city, area, city_size, priority, search_query,
               status, lat, lon, radius_m, kind, source, source_ref, created_run, geocode_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (next_id + i, place_id, city["country"], city["name"], area, city["size_code"], city["priority"],
             None, "Not started", lat, lon, radius, kind, ref.split(":")[0] or "geonames", ref, run_id, "matched"),
        )
    return res


def _surrounding_cities(conn, city, min_pop, km, inner_km, run_id, dry_run) -> list[str]:
    """Large places beyond the city's own zone radius that the Cities tab does not list yet."""
    known = {
        norm_place(r["name"])
        for r in conn.execute("SELECT name FROM geo_place WHERE country=? AND level='city'", (city["country"],))
    }
    added = []
    for d, p in _places_near(conn, city["iso2"], city["lat"], city["lon"], km, min_pop):
        key = norm_place(p["name"])
        if key in known or d <= inner_km:
            continue
        known.add(key)
        added.append(p["name"])
        if not dry_run:
            conn.execute(
                """INSERT INTO geo_place(place_id, parent_id, level, name, country, state_province, size_code,
                   status, notes, lat, lon, source, source_ref, last_reviewed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,date('now'))""",
                (new_ulid(), city["parent_id"], "city", p["name"], city["country"], city["state_province"],
                 "S", "Not started", f"Added near {city['name']} ({d:.0f} km) by {run_id}",
                 p["lat"], p["lon"], "geonames", f"geonames:{p['geonameid']}"),
            )
    return added
