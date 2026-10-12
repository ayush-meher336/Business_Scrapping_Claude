-- v3: discovery candidates, identity keys for deduplication, merge log and review queue.

ALTER TABLE business ADD COLUMN domain TEXT;            -- registrable domain of the official website
ALTER TABLE business ADD COLUMN phone_e164 TEXT;
ALTER TABLE business ADD COLUMN name_norm TEXT;
ALTER TABLE business ADD COLUMN group_domain TEXT;      -- branches sharing one website
ALTER TABLE business ADD COLUMN maps_category TEXT;
ALTER TABLE business ADD COLUMN maps_rating REAL;
ALTER TABLE business ADD COLUMN permanently_closed INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_business_domain ON business(domain);
CREATE INDEX IF NOT EXISTS ix_business_phone ON business(phone_e164);
CREATE INDEX IF NOT EXISTS ix_business_place ON business(google_place_id);
CREATE INDEX IF NOT EXISTS ix_business_name ON business(country, name_norm);

-- Every raw hit from a collector, kept even when it is rejected or merged.
CREATE TABLE IF NOT EXISTS candidate (
    candidate_id INTEGER PRIMARY KEY,
    job_id       INTEGER REFERENCES search_job(job_id),
    source       TEXT NOT NULL,                 -- google_maps_browser | web_search_browser | ...
    source_ref   TEXT,                          -- Maps place id, or result URL
    name         TEXT,
    category     TEXT,
    address      TEXT,
    phone        TEXT,
    website      TEXT,
    lat          REAL,
    lon          REAL,
    rating       REAL,
    reviews      INTEGER,
    permanently_closed INTEGER NOT NULL DEFAULT 0,
    maps_url     TEXT,
    snippet      TEXT,
    raw_json     TEXT,
    fetched_at   TEXT NOT NULL,
    lead_key     TEXT REFERENCES business(lead_key),
    resolution   TEXT,                          -- new | merged | review | rejected
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS ix_candidate_job ON candidate(job_id);
CREATE INDEX IF NOT EXISTS ix_candidate_lead ON candidate(lead_key);
CREATE INDEX IF NOT EXISTS ix_candidate_resolution ON candidate(resolution);

CREATE TABLE IF NOT EXISTS merge_log (
    merge_id     INTEGER PRIMARY KEY,
    lead_key     TEXT NOT NULL REFERENCES business(lead_key),
    candidate_id INTEGER REFERENCES candidate(candidate_id),
    rule         TEXT NOT NULL,
    score        REAL,
    decided_by   TEXT NOT NULL DEFAULT 'auto',  -- auto | human
    created_at   TEXT NOT NULL
);

-- Possible duplicates that need a human decision (fuzzy name match, no hard identifier).
CREATE TABLE IF NOT EXISTS dedup_review (
    review_id    INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidate(candidate_id),
    lead_key     TEXT NOT NULL REFERENCES business(lead_key),   -- the new record
    match_key    TEXT NOT NULL REFERENCES business(lead_key),   -- the record it may duplicate
    score        REAL NOT NULL,
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open', -- open | same | different
    created_at   TEXT NOT NULL
);
