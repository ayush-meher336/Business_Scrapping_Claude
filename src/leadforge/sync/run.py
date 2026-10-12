"""One sync pass: Leads, Search Zones, Cities (two-way), Lead_Sources (append-only)."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

from leadforge.core.ids import new_ulid
from leadforge.niches.model import NicheConfig
from leadforge.sync.client import SheetClient
from leadforge.sync.engine import append_only, sync_tab
from leadforge.sync.tabs import SOURCE_HEADERS, cities_spec, leads_spec, sources_after, zones_spec


def run_sync(conn: sqlite3.Connection, client: SheetClient, niche: NicheConfig,
             tabs: tuple[str, ...] = ("Leads", "Search Zones", "Cities", "Lead_Sources")) -> dict:
    run_id = f"sync-{new_ulid()}"
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn.execute("INSERT INTO run(run_id, kind, started_at) VALUES (?,?,?)", (run_id, "sync", started))
    conn.commit()
    out: dict = {}
    specs = {"Leads": lambda: leads_spec(niche), "Search Zones": zones_spec, "Cities": cities_spec}
    for tab in tabs:
        if tab in specs:
            out[tab] = sync_tab(conn, client, specs[tab](), run_id).__dict__
        elif tab == "Lead_Sources":
            out[tab] = {"appended_rows": append_only(conn, client, "Lead_Sources", SOURCE_HEADERS,
                                                     "lead_sources:last_id", sources_after, run_id)}
    conn.execute("UPDATE run SET finished_at=?, stats_json=? WHERE run_id=?",
                 (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), json.dumps(out), run_id))
    conn.commit()
    return out
