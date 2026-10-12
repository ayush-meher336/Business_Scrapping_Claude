"""Find industrial estates and business parks around a city on Google Maps (browser).

Results are stored as area features (refs `gmaps:<place id>`) that the zone builder uses as
industrial evidence and as candidate industrial zones.
"""
from __future__ import annotations

import re
import sqlite3

from leadforge.geography.osm import store

QUERIES = ["industrial estate {city}", "industrial area {city}", "industrial park {city}", "business park {city}"]
AREA_CATEGORY = re.compile(r"(?i)\b(industrial (estate|area|park|zone)|business park|business center|"
                           r"business centre|office park|corporate campus|logistics park|special economic zone)\b")
AREA_NAME = re.compile(r"(?i)\b(industrial|estate|business park|precinct|ind\.? area|midc|gidc|sidco|"
                       r"kawasan industri|zone industrielle)\b")


GENERIC = re.compile(r"(?i)^\W*(the\s+)?(industrial|business|logistics)\s+(park|area|estate|zone)\W*$")


def is_area(result: dict) -> bool:
    if GENERIC.match(result.get("name") or ""):
        return False                     # a bare "Industrial Park" names nothing a search can target
    return bool(AREA_CATEGORY.search(result.get("category") or "") or AREA_NAME.search(result.get("name") or ""))


def scan_city(conn: sqlite3.Connection, session, place_id: str, city: str, country: str) -> int:
    seen: dict[str, dict] = {}
    for q in QUERIES:
        for r in session.search(q.format(city=f"{city} {country}")):
            r = r.as_dict() if hasattr(r, "as_dict") else r
            if is_area(r) and r.get("lat") is not None and (r.get("place_id") or r.get("maps_url")):
                seen[r.get("place_id") or r["maps_url"]] = r
    feats = [{"osm_ref": f"gmaps:{ref}", "kind": "industrial", "name": r["name"], "lat": r["lat"], "lon": r["lon"]}
             for ref, r in seen.items()]
    with conn:
        store(conn, place_id, feats)
    return len(feats)
