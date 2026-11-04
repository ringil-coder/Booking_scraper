-- Robocza baza SQLite (A§9). Odtwarzana z manifestów i partii na gałęzi monitor-data.
-- Kwoty w groszach (INTEGER). Daty pobytów jako 'YYYY-MM-DD', momenty jako ISO 8601 UTC.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS own_apartment (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    booking_url TEXT
);

CREATE TABLE IF NOT EXISTS competitors (
    id                  TEXT PRIMARY KEY,          -- competitor_key z competitors.csv
    name                TEXT NOT NULL,
    canonical_url       TEXT NOT NULL,
    booking_url         TEXT NOT NULL,
    booking_property_id TEXT,
    unit_filter         TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS search_profiles (
    profile_hash       TEXT PRIMARY KEY,
    adults             INTEGER NOT NULL,
    children_ages      TEXT NOT NULL,              -- JSON
    units              INTEGER NOT NULL,
    currency           TEXT NOT NULL,
    locale             TEXT NOT NULL,
    device             TEXT NOT NULL,
    logged_in          INTEGER NOT NULL,
    accommodation_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    id                 TEXT PRIMARY KEY,
    schedule_slot      INTEGER NOT NULL,
    cycle_date         TEXT NOT NULL,
    start_date         TEXT NOT NULL,
    end_date_exclusive TEXT NOT NULL,
    stay_nights        TEXT NOT NULL,              -- JSON
    status             TEXT NOT NULL,
    profile_hash       TEXT NOT NULL REFERENCES search_profiles(profile_hash),
    config_hash        TEXT NOT NULL,
    code_sha           TEXT,
    parser_version     TEXT NOT NULL,
    query_count        INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    blocked_reason     TEXT,
    blocked_message    TEXT
);

CREATE TABLE IF NOT EXISTS cycle_competitors (
    cycle_id      TEXT NOT NULL REFERENCES cycles(id),
    competitor_id TEXT NOT NULL REFERENCES competitors(id),
    name          TEXT NOT NULL,
    unit_filter   TEXT,
    PRIMARY KEY (cycle_id, competitor_id)
);

CREATE TABLE IF NOT EXISTS stay_queries (
    id            TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL REFERENCES competitors(id),
    checkin       TEXT NOT NULL,
    checkout      TEXT NOT NULL,
    nights        INTEGER NOT NULL,
    profile_hash  TEXT NOT NULL REFERENCES search_profiles(profile_hash),
    source        TEXT NOT NULL,
    UNIQUE (competitor_id, checkin, checkout, profile_hash, source)
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,               -- '<cycle_id>:<query_id>'
    cycle_id        TEXT NOT NULL REFERENCES cycles(id),
    query_id        TEXT NOT NULL REFERENCES stay_queries(id),
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_until     TEXT,
    last_reason     TEXT,
    last_message    TEXT,
    sort_key        INTEGER NOT NULL DEFAULT 0,     -- kolejność wykonywania (P4)
    UNIQUE (cycle_id, query_id)
);
CREATE INDEX IF NOT EXISTS jobs_cycle_status ON jobs (cycle_id, status, sort_key);

CREATE TABLE IF NOT EXISTS attempts (
    id                   TEXT PRIMARY KEY,          -- '<job_id>#<attempt_no>'
    job_id               TEXT NOT NULL REFERENCES jobs(id),
    cycle_id             TEXT NOT NULL REFERENCES cycles(id),
    attempt_no           INTEGER NOT NULL,
    started_at           TEXT NOT NULL,
    finished_at          TEXT NOT NULL,
    workflow_run_id      TEXT,
    workflow_run_attempt INTEGER,
    code_sha             TEXT,
    runner_environment   TEXT,
    technical_reason     TEXT,
    message              TEXT,
    job_status_after     TEXT NOT NULL,
    batch_id             TEXT,
    UNIQUE (job_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS observations (
    id             TEXT PRIMARY KEY,
    attempt_id     TEXT NOT NULL UNIQUE REFERENCES attempts(id),
    query_id       TEXT NOT NULL REFERENCES stay_queries(id),
    cycle_id       TEXT NOT NULL REFERENCES cycles(id),
    observed_at    TEXT NOT NULL,
    availability   TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    source_url     TEXT,
    message        TEXT
);
CREATE INDEX IF NOT EXISTS observations_query ON observations (query_id, observed_at);
CREATE INDEX IF NOT EXISTS observations_cycle ON observations (cycle_id);

CREATE TABLE IF NOT EXISTS offers (
    id                      INTEGER PRIMARY KEY,
    observation_id          TEXT NOT NULL REFERENCES observations(id),
    offer_index             INTEGER NOT NULL,
    unit_name               TEXT NOT NULL,
    unit_id                 TEXT,
    is_entire_apartment     INTEGER,
    max_occupancy           INTEGER,
    units                   INTEGER NOT NULL,
    rate_name               TEXT,
    rate_category           TEXT NOT NULL,
    free_cancellation_until TEXT,
    meal_plan               TEXT NOT NULL,
    prepayment              TEXT NOT NULL,
    discounts               TEXT NOT NULL,          -- JSON
    currency                TEXT NOT NULL,
    displayed_price_grosze  INTEGER NOT NULL,
    original_price_grosze   INTEGER,
    total_price_grosze      INTEGER,
    avg_night_price_grosze  INTEGER,
    price_completeness      TEXT NOT NULL,
    deposit_grosze          INTEGER,
    matches_unit_filter     INTEGER NOT NULL,
    selected_for_category   INTEGER NOT NULL,
    UNIQUE (observation_id, offer_index)
);

CREATE TABLE IF NOT EXISTS price_components (
    id                 INTEGER PRIMARY KEY,
    offer_id           INTEGER NOT NULL REFERENCES offers(id),
    name               TEXT NOT NULL,
    kind               TEXT NOT NULL,
    basis              TEXT NOT NULL,
    amount_grosze      INTEGER,
    rate_percent       TEXT,
    mandatory          INTEGER NOT NULL,
    included           INTEGER NOT NULL,
    stay_amount_grosze INTEGER
);

CREATE TABLE IF NOT EXISTS imported_batches (
    batch_id    TEXT PRIMARY KEY,
    cycle_id    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    events      INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
