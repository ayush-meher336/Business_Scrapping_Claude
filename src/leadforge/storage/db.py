"""SQLite connection, schema setup and migrations."""
from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

# Numbered files in storage/migrations/ upgrade the base schema (v1) one version at a time.
MIGRATIONS = {2: "002_geography.sql", 3: "003_discovery.sql", 4: "004_enrichment.sql", 5: "005_ai.sql"}
SCHEMA_VERSION = max(MIGRATIONS)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)  # wait for other writers (e.g. a background fetch)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _sql(name: str) -> str:
    return resources.files("leadforge.storage").joinpath(name).read_text(encoding="utf-8")


def version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_schema(conn: sqlite3.Connection) -> None:
    if version(conn) == 0:
        conn.executescript(_sql("schema.sql"))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    upgrade(conn)


def upgrade(conn: sqlite3.Connection) -> list[int]:
    applied = []
    for v in sorted(MIGRATIONS):
        if version(conn) < v:
            conn.executescript("BEGIN;" + _sql(f"migrations/{MIGRATIONS[v]}") + f"PRAGMA user_version = {v};COMMIT;")
            applied.append(v)
    return applied


def is_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='legacy_row'"
    ).fetchone()
    if row is None:
        return True
    return conn.execute("SELECT COUNT(*) FROM legacy_row").fetchone()[0] == 0
