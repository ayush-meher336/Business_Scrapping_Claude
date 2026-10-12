"""Stored area features (industrial estates, suburbs, ports) used as zone evidence.

No API is called from here. The table was first seeded with features for 9 cities from an
earlier OpenStreetMap fetch; from Phase 3 the Google Maps browser scan (SeleniumBase) adds
industrial areas with refs prefixed `gmaps:`.
"""
from __future__ import annotations

import datetime as dt
import sqlite3


def store(conn: sqlite3.Connection, place_id: str, features: list[dict]) -> int:
    """Upsert features: dicts with osm_ref (any unique ref), kind, name, lat, lon."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO osm_feature VALUES (?,?,?,?,?,?,?)",
        [(f["osm_ref"], place_id, f["kind"], f["name"], f["lat"], f["lon"], now) for f in features],
    )
    return len(features)
