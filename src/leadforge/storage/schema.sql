-- leadforge schema v1 (Phase 1: migration foundation).
-- Later phases add task queues, pages, AI results and sync tables.

PRAGMA foreign_keys = ON;

-- Lossless copy of every non-empty workbook row (cached values, not formulas).
CREATE TABLE IF NOT EXISTS legacy_row (
    tab         TEXT    NOT NULL,
    row_num     INTEGER NOT NULL,          -- 1-based sheet row
    values_json TEXT    NOT NULL,          -- JSON array, one entry per column
    PRIMARY KEY (tab, row_num)
);

-- The formula each column used, taken from the first data row, kept for reference
-- while the logic moves into Python.
CREATE TABLE IF NOT EXISTS legacy_formula (
    tab     TEXT NOT NULL,
    column  TEXT NOT NULL,
    formula TEXT NOT NULL,
    PRIMARY KEY (tab, column)
);

CREATE TABLE IF NOT EXISTS run (
    run_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,             -- migrate | research | sync | legacy
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    config_json TEXT,
    stats_json  TEXT
);

-- Controlled vocabularies from the Lists tab.
CREATE TABLE IF NOT EXISTS enum_value (
    list_name TEXT    NOT NULL,
    position  INTEGER NOT NULL,
    value     TEXT    NOT NULL,
    PRIMARY KEY (list_name, value)
);

CREATE TABLE IF NOT EXISTS city_size (
    code         TEXT PRIMARY KEY,         -- X | M | H | S
    meaning      TEXT NOT NULL,
    target_leads INTEGER
);

CREATE TABLE IF NOT EXISTS source (
    name          TEXT PRIMARY KEY,
    scope         TEXT,
    best_for      TEXT,
    where_how     TEXT,
    typical_yield TEXT
);

CREATE TABLE IF NOT EXISTS compliance_rule (
    market          TEXT PRIMARY KEY,
    law             TEXT,
    b2b_cold_email  TEXT,
    requirements    TEXT
);

CREATE TABLE IF NOT EXISTS country (
    name                 TEXT PRIMARY KEY,
    tier                 INTEGER,
    region               TEXT,
    trade_role           TEXT,
    languages            TEXT,
    local_keywords       TEXT,
    cold_email_rule      TEXT,
    notes                TEXT,
    legacy_target_leads  INTEGER,
    legacy_row           INTEGER NOT NULL
);

-- Geographic hierarchy. Phase 1 fills country and city levels from the workbook;
-- Phase 2 adds regions, localities and coordinates from GeoNames / OSM.
CREATE TABLE IF NOT EXISTS geo_place (
    place_id       TEXT PRIMARY KEY,
    parent_id      TEXT REFERENCES geo_place(place_id),
    level          TEXT NOT NULL,          -- country | region | city | locality
    name           TEXT NOT NULL,
    country        TEXT NOT NULL,
    state_province TEXT,
    size_code      TEXT,
    city_type      TEXT,
    who_to_target  TEXT,
    areas_to_search TEXT,
    industry_profile TEXT,
    suggested_searches TEXT,
    priority       TEXT,
    status         TEXT,
    assigned_to    TEXT,
    notes          TEXT,
    lat            REAL,
    lon            REAL,
    source         TEXT NOT NULL,          -- legacy_workbook | geonames | osm | ...
    source_ref     TEXT,
    legacy_loc_id  INTEGER UNIQUE,
    last_reviewed  TEXT
);
CREATE INDEX IF NOT EXISTS ix_geo_place_name ON geo_place(country, name, level);

CREATE TABLE IF NOT EXISTS zone (
    zone_id        INTEGER PRIMARY KEY,    -- legacy Zone IDs kept; new zones continue the sequence
    place_id       TEXT REFERENCES geo_place(place_id),
    country        TEXT NOT NULL,
    city           TEXT NOT NULL,
    area           TEXT NOT NULL,
    city_size      TEXT,
    priority       TEXT,
    search_query   TEXT,
    status         TEXT,
    assigned_to    TEXT,
    date_done      TEXT,
    notes          TEXT,
    legacy_leads_collected INTEGER,
    blocked_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_zone_place ON zone(place_id);

-- One physical business location.
CREATE TABLE IF NOT EXISTS business (
    lead_key            TEXT PRIMARY KEY,  -- ULID, permanent
    lead_id             INTEGER NOT NULL UNIQUE, -- shown in the sheet; frozen from legacy, new ones continue
    region              TEXT,
    country             TEXT,
    state_province      TEXT,
    city                TEXT,
    suburb_area         TEXT,
    company_name        TEXT NOT NULL,
    website             TEXT,
    located_in_zone     TEXT,
    street_address      TEXT,
    postcode            TEXT,
    business_reg_no     TEXT,
    registered_name     TEXT,
    registration_status TEXT,
    industry_type       TEXT,
    material_focus      TEXT,
    company_size        TEXT,
    general_email       TEXT,
    decision_maker_name TEXT,
    decision_maker_role TEXT,
    decision_maker_email TEXT,
    email_type          TEXT,
    phone               TEXT,
    linkedin_company_page TEXT,
    source_url          TEXT,
    source_type         TEXT,
    found_via_keyword   TEXT,
    opt_out_notice      TEXT,
    email_verified      TEXT,
    priority            TEXT,
    outreach_status     TEXT,
    last_contact_date   TEXT,
    date_collected      TEXT,
    collected_by        TEXT,
    notes               TEXT,
    legacy_duplicate_flag TEXT,            -- cached value of the old COUNTIF formula
    legacy_ok_to_email  TEXT,              -- cached value of the old OK-to-email formula
    website_status      TEXT,
    last_verified       TEXT,
    -- New fields (filled from Phase 3 onwards)
    company_description TEXT,
    personalisation_hook TEXT,
    contact_page_url    TEXT,
    google_maps_url     TEXT,
    google_place_id     TEXT,
    lat                 REAL,
    lon                 REAL,
    zone_id             INTEGER REFERENCES zone(zone_id),
    research_status     TEXT NOT NULL DEFAULT 'legacy',
    confidence          TEXT,
    human_review_required TEXT,
    review_notes        TEXT,
    dedup_cluster_id    TEXT,
    first_discovered_at TEXT,
    last_researched_at  TEXT,
    last_updated_at     TEXT,
    legacy_row          INTEGER            -- sheet row at migration time
);
CREATE INDEX IF NOT EXISTS ix_business_geo ON business(country, city, suburb_area);
CREATE INDEX IF NOT EXISTS ix_business_website ON business(website);

CREATE TABLE IF NOT EXISTS contact (
    contact_id    INTEGER PRIMARY KEY,
    lead_key      TEXT NOT NULL REFERENCES business(lead_key),
    kind          TEXT NOT NULL,           -- email | phone | form
    value         TEXT NOT NULL,
    role          TEXT,                    -- general | decision_maker
    role_based    INTEGER,
    syntax_ok     INTEGER,
    verify_status TEXT,
    source_record_id INTEGER REFERENCES source_record(source_record_id),
    UNIQUE (lead_key, kind, value)
);

CREATE TABLE IF NOT EXISTS source_record (
    source_record_id INTEGER PRIMARY KEY,
    lead_key         TEXT NOT NULL REFERENCES business(lead_key),
    source_category  TEXT,
    source_name      TEXT,
    source_url       TEXT,
    evidence_type    TEXT,
    fields_supported TEXT,
    date_accessed    TEXT,
    verification_status TEXT,
    confidence       TEXT,
    run_id           TEXT REFERENCES run(run_id),
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_record_lead ON source_record(lead_key);

CREATE TABLE IF NOT EXISTS research_log (
    log_id        INTEGER PRIMARY KEY,
    run_id        TEXT REFERENCES run(run_id),
    date          TEXT,
    country       TEXT,
    city          TEXT,
    zone          TEXT,
    keyword       TEXT,
    source        TEXT,
    results_reviewed TEXT,
    new_companies_added TEXT,
    notes         TEXT,
    legacy_row    INTEGER
);

CREATE TABLE IF NOT EXISTS suppression (
    value      TEXT PRIMARY KEY,           -- email address or domain
    kind       TEXT NOT NULL,              -- email | domain
    reason     TEXT NOT NULL,              -- unsubscribed | bounced | do_not_contact
    added_at   TEXT NOT NULL,
    lead_key   TEXT
);
