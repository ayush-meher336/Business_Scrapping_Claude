-- v2: geography engine, keyword library, search planner.

ALTER TABLE country ADD COLUMN iso2 TEXT;
ALTER TABLE country ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE country ADD COLUMN excluded_reason TEXT;

ALTER TABLE zone ADD COLUMN lat REAL;
ALTER TABLE zone ADD COLUMN lon REAL;
ALTER TABLE zone ADD COLUMN radius_m INTEGER;
ALTER TABLE zone ADD COLUMN kind TEXT NOT NULL DEFAULT 'suburb';   -- suburb | industrial | town | city
ALTER TABLE zone ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy_workbook';
ALTER TABLE zone ADD COLUMN source_ref TEXT;
ALTER TABLE zone ADD COLUMN alias_of INTEGER REFERENCES zone(zone_id);
ALTER TABLE zone ADD COLUMN created_run TEXT;
ALTER TABLE zone ADD COLUMN geocode_status TEXT;                    -- matched | ambiguous | unmatched

-- GeoNames populated places (cities500 + admin names), loaded per country.
CREATE TABLE IF NOT EXISTS gn_place (
    geonameid    INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    asciiname    TEXT,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    feature_code TEXT,
    country_code TEXT NOT NULL,
    admin1_code  TEXT,
    population   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_gn_place_cc ON gn_place(country_code);

CREATE TABLE IF NOT EXISTS gn_name (
    geonameid INTEGER NOT NULL REFERENCES gn_place(geonameid),
    name_norm TEXT NOT NULL,
    PRIMARY KEY (name_norm, geonameid)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS gn_admin1 (
    code      TEXT PRIMARY KEY,                -- e.g. AU.02
    name      TEXT NOT NULL,
    asciiname TEXT
);

CREATE TABLE IF NOT EXISTS gn_country (
    iso2       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    languages  TEXT,
    phone      TEXT,
    population INTEGER
);

-- OSM features (industrial estates, suburbs) fetched per city.
CREATE TABLE IF NOT EXISTS osm_feature (
    osm_ref   TEXT PRIMARY KEY,                -- way/123, node/456, relation/789
    place_id  TEXT REFERENCES geo_place(place_id),
    kind      TEXT NOT NULL,                   -- industrial | suburb | neighbourhood | port
    name      TEXT,
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keyword (
    keyword_id INTEGER PRIMARY KEY,
    niche      TEXT NOT NULL,
    language   TEXT NOT NULL,
    country    TEXT,                           -- NULL = applies to every country using the language
    term       TEXT NOT NULL,
    term_norm  TEXT NOT NULL,
    kind       TEXT NOT NULL,                  -- core | synonym | material | local | exclusion
    source     TEXT NOT NULL,                  -- niche_config | countries_tab | ai_proposed
    active     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (niche, language, country, term_norm)
);

-- Historic yield per keyword, by country and city size (from the research log, then live runs).
CREATE TABLE IF NOT EXISTS keyword_yield (
    keyword_id  INTEGER NOT NULL REFERENCES keyword(keyword_id),
    country     TEXT NOT NULL,
    size_code   TEXT NOT NULL DEFAULT '*',
    source      TEXT NOT NULL,
    queries     INTEGER NOT NULL DEFAULT 0,
    results     INTEGER NOT NULL DEFAULT 0,
    new_uniques INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (keyword_id, country, size_code, source)
);

CREATE TABLE IF NOT EXISTS search_job (
    job_id          INTEGER PRIMARY KEY,
    run_id          TEXT REFERENCES run(run_id),
    zone_id         INTEGER NOT NULL REFERENCES zone(zone_id),
    keyword_id      INTEGER NOT NULL REFERENCES keyword(keyword_id),
    source          TEXT NOT NULL,
    query           TEXT NOT NULL,
    round           INTEGER NOT NULL DEFAULT 1,
    priority        REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | claimed | done | failed | blocked
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    claimed_by      TEXT,
    claimed_at      TEXT,
    results         INTEGER,
    new_uniques     INTEGER,
    error           TEXT,
    created_at      TEXT NOT NULL,
    finished_at     TEXT,
    UNIQUE (zone_id, keyword_id, source)
);
CREATE INDEX IF NOT EXISTS ix_search_job_queue ON search_job(status, priority DESC);
