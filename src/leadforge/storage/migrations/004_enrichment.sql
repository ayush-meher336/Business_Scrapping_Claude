-- v4: website enrichment (pages, contact evidence, email checks).

ALTER TABLE business ADD COLUMN additional_emails TEXT;   -- '; '-separated
ALTER TABLE business ADD COLUMN additional_phones TEXT;
ALTER TABLE business ADD COLUMN postcode_source TEXT;

ALTER TABLE contact ADD COLUMN source_url TEXT;
ALTER TABLE contact ADD COLUMN context TEXT;              -- mailto | tel | jsonld | text | footer
ALTER TABLE contact ADD COLUMN on_own_domain INTEGER;
ALTER TABLE contact ADD COLUMN mx_ok INTEGER;

-- One row per business per enrichment attempt state.
CREATE TABLE IF NOT EXISTS enrich_task (
    lead_key    TEXT PRIMARY KEY REFERENCES business(lead_key),
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | blocked | needs_browser
    attempts    INTEGER NOT NULL DEFAULT 0,
    pages       INTEGER,
    used_browser INTEGER NOT NULL DEFAULT 0,
    ms          INTEGER,
    error       TEXT,
    run_id      TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_enrich_status ON enrich_task(status);

CREATE TABLE IF NOT EXISTS page (
    url_hash    TEXT PRIMARY KEY,
    lead_key    TEXT NOT NULL REFERENCES business(lead_key),
    url         TEXT NOT NULL,
    final_url   TEXT,
    http_status INTEGER,
    page_type   TEXT,                             -- home | contact | about | products | location | other
    fetched_at  TEXT NOT NULL,
    ms          INTEGER,
    via_browser INTEGER NOT NULL DEFAULT 0,
    text        TEXT,                             -- cleaned main text (trafilatura), trimmed
    jsonld      TEXT,                             -- Organization / LocalBusiness objects found
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_page_lead ON page(lead_key);

-- MX lookups are per domain; cache them.
CREATE TABLE IF NOT EXISTS mx_cache (
    domain     TEXT PRIMARY KEY,
    mx_ok      INTEGER NOT NULL,
    hosts      TEXT,
    checked_at TEXT NOT NULL
);
