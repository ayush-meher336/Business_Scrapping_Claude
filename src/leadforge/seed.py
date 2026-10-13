"""Reference-data seed: lets a new machine start from the GitHub repo alone.

Contains no lead or contact data: countries, cities, zones, controlled lists, compliance rules,
sources and keywords. Leads come from the shared Google Sheet on the first sync.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

TABLES = ["country", "enum_value", "city_size", "source", "compliance_rule", "geo_place", "zone", "keyword"]
SEED_PATH = Path("seed/reference.json.gz")


def export_seed(conn: sqlite3.Connection, root: Path) -> tuple[Path, dict[str, int]]:
    data, counts = {}, {}
    for t in TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        rows = [list(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {t}")]
        data[t] = {"columns": cols, "rows": rows}
        counts[t] = len(rows)
    path = root / SEED_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    return path, counts


def import_seed(conn: sqlite3.Connection, root: Path) -> dict[str, int]:
    with gzip.open(root / SEED_PATH, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    counts = {}
    conn.execute("PRAGMA foreign_keys = OFF")          # geo_place rows reference each other in any order
    with conn:
        for t in TABLES:
            if t not in data:
                continue
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            cols = [c for c in data[t]["columns"] if c in have]
            idx = [data[t]["columns"].index(c) for c in cols]
            rows = [[r[i] for i in idx] for r in data[t]["rows"]]
            conn.executemany(f"INSERT OR IGNORE INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
            counts[t] = len(rows)
    conn.execute("PRAGMA foreign_keys = ON")
    return counts
