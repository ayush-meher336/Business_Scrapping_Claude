-- v6: Google Sheets sync (the sheet is the shared record between machines).

-- What each tab row looked like after the last confirmed sync. Comparing the live sheet and the
-- database against this snapshot tells who changed what since then (three-way merge).
CREATE TABLE IF NOT EXISTS sheet_snapshot (
    tab         TEXT NOT NULL,
    row_key     TEXT NOT NULL,
    values_json TEXT NOT NULL,      -- {header: value} as written / read
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (tab, row_key)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,         -- e.g. lead_sources:last_id
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    sync_id     INTEGER PRIMARY KEY,
    run_id      TEXT,
    tab         TEXT NOT NULL,
    action      TEXT NOT NULL,      -- pull | update | append | header
    rows        INTEGER NOT NULL,
    detail      TEXT,
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_conflict (
    conflict_id INTEGER PRIMARY KEY,
    tab         TEXT NOT NULL,
    row_key     TEXT NOT NULL,
    field       TEXT NOT NULL,
    db_value    TEXT,
    sheet_value TEXT,
    resolution  TEXT NOT NULL,      -- sheet_won | appended
    at          TEXT NOT NULL
);
