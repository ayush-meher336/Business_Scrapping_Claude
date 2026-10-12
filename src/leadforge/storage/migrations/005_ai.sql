-- v5: agent-run AI enrichment (batch export / validated import).

CREATE TABLE IF NOT EXISTS ai_batch (
    batch_id       TEXT PRIMARY KEY,
    prompt_version TEXT NOT NULL,
    niche          TEXT NOT NULL,
    path           TEXT NOT NULL,          -- exported batch file
    status         TEXT NOT NULL DEFAULT 'exported',   -- exported | imported | partial
    created_at     TEXT NOT NULL,
    imported_at    TEXT
);

CREATE TABLE IF NOT EXISTS ai_task (
    lead_key   TEXT NOT NULL REFERENCES business(lead_key),
    batch_id   TEXT NOT NULL REFERENCES ai_batch(batch_id),
    status     TEXT NOT NULL DEFAULT 'exported',      -- exported | accepted | review | rejected | missing
    evidence   TEXT NOT NULL,                         -- JSON: the evidence the agent saw (for grounding checks)
    PRIMARY KEY (lead_key, batch_id)
);
CREATE INDEX IF NOT EXISTS ix_ai_task_status ON ai_task(status);

CREATE TABLE IF NOT EXISTS ai_result (
    lead_key        TEXT NOT NULL REFERENCES business(lead_key),
    batch_id        TEXT NOT NULL REFERENCES ai_batch(batch_id),
    prompt_version  TEXT NOT NULL,
    output_json     TEXT NOT NULL,                    -- what the agent returned, as validated
    grounding_ok    INTEGER NOT NULL,
    problems        TEXT,                             -- '; '-separated validation findings
    decision        TEXT NOT NULL,                    -- accepted | review | rejected
    imported_at     TEXT NOT NULL,
    PRIMARY KEY (lead_key, batch_id)
);
