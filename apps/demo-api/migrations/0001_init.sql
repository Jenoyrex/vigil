-- Vigil public demo schema (Cloudflare D1 / SQLite).
--
-- Mirrors the tables the dashboard-facing /v1 contract needs from apps/api's
-- PostgreSQL models and the two ClickHouse tables (infrastructure/clickhouse/
-- init). See docs/decisions/009-public-demo-architecture.md. Conventions:
--   * ids are canonical lowercase UUID text; timestamps are epoch milliseconds
--     (UTC), matching ClickHouse DateTime64(3) precision;
--   * ClickHouse Map/Nested columns are JSON text;
--   * Decimal64(6) costs are integer micro-dollars (*_micros);
--   * booleans are 0/1.
-- Foreign keys cascade so the scheduled cleanup can delete a stale user or
-- organization and everything beneath it in one statement.

CREATE TABLE users (
  id              TEXT PRIMARY KEY,
  email           TEXT NOT NULL UNIQUE,  -- stored normalized (strip + lower)
  full_name       TEXT,
  hashed_password TEXT NOT NULL,         -- demo-only PBKDF2 (src/passwords.ts)
  is_active       INTEGER NOT NULL DEFAULT 1,
  created_at      INTEGER NOT NULL
);

CREATE TABLE dashboard_sessions (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,       -- sha256 hex of the raw token
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX ix_dashboard_sessions_user ON dashboard_sessions (user_id, created_at);

CREATE TABLE organizations (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  slug       TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
);

CREATE TABLE organization_memberships (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
  created_at      INTEGER NOT NULL,
  UNIQUE (user_id, organization_id)
);
CREATE INDEX ix_memberships_org ON organization_memberships (organization_id);

CREATE TABLE projects (
  id              TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  slug            TEXT NOT NULL,
  created_at      INTEGER NOT NULL
);
CREATE INDEX ix_projects_org ON projects (organization_id, created_at);

CREATE TABLE api_keys (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  key_prefix   TEXT NOT NULL,
  key_hash     TEXT NOT NULL UNIQUE,     -- sha256 hex of the raw key
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  created_by   TEXT,
  created_at   INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at   INTEGER
);
CREATE INDEX ix_api_keys_project ON api_keys (project_id, created_at);

CREATE TABLE evaluator_configs (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  evaluator_name TEXT NOT NULL,
  enabled        INTEGER NOT NULL DEFAULT 0,
  sampling_rate  REAL NOT NULL,
  threshold      REAL,
  max_retries    INTEGER NOT NULL,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  UNIQUE (project_id, evaluator_name)
);

-- ClickHouse `spans` (ReplacingMergeTree keyed by project/trace/span: a
-- re-sent span replaces the stored one).
CREATE TABLE spans (
  project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  trace_id             TEXT NOT NULL,
  span_id              TEXT NOT NULL,
  parent_span_id       TEXT,
  name                 TEXT NOT NULL,
  span_type            TEXT NOT NULL,
  resource             TEXT NOT NULL,
  start_time           INTEGER NOT NULL,
  end_time             INTEGER NOT NULL,
  duration_ms          INTEGER NOT NULL,
  status               TEXT NOT NULL CHECK (status IN ('unset', 'ok', 'error')),
  status_message       TEXT,
  input                TEXT,
  input_size_bytes     INTEGER NOT NULL,
  input_truncated      INTEGER NOT NULL,
  output               TEXT,
  output_size_bytes    INTEGER NOT NULL,
  output_truncated     INTEGER NOT NULL,
  attributes           TEXT NOT NULL,    -- JSON object of string -> string
  attributes_truncated INTEGER NOT NULL,
  events               TEXT NOT NULL,    -- JSON [{time_ms, name, attributes}]
  events_truncated     INTEGER NOT NULL,
  llm_provider         TEXT,
  llm_model            TEXT,
  llm_input_tokens     INTEGER,
  llm_output_tokens    INTEGER,
  llm_total_tokens     INTEGER,
  llm_cost_micros      INTEGER,
  environment          TEXT NOT NULL,
  release              TEXT,
  ingested_at          INTEGER NOT NULL,
  PRIMARY KEY (project_id, trace_id, span_id)
);
CREATE INDEX ix_spans_project_start ON spans (project_id, start_time);
CREATE INDEX ix_spans_ingested ON spans (ingested_at);

CREATE TABLE evaluation_jobs (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  trace_id          TEXT NOT NULL,
  span_id           TEXT NOT NULL,
  evaluator_name    TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  status            TEXT NOT NULL
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'dead_letter')),
  attempt_count     INTEGER NOT NULL,
  max_retries       INTEGER NOT NULL,
  next_attempt_at   INTEGER,
  claimed_at        INTEGER,
  claimed_by        TEXT,
  last_error        TEXT,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,
  UNIQUE (project_id, trace_id, span_id, evaluator_name, evaluator_version)
);
CREATE INDEX ix_evaluation_jobs_list ON evaluation_jobs (project_id, created_at, id);

-- ClickHouse `evaluation_results`; evaluation_id is the job id (as in
-- services/worker's result_mapping).
CREATE TABLE evaluation_results (
  evaluation_id          TEXT PRIMARY KEY REFERENCES evaluation_jobs(id) ON DELETE CASCADE,
  project_id             TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  trace_id               TEXT NOT NULL,
  span_id                TEXT NOT NULL,
  evaluator_name         TEXT NOT NULL,
  evaluator_version      TEXT NOT NULL,
  score                  REAL,
  label                  TEXT NOT NULL,
  explanation            TEXT NOT NULL,
  evaluator_model        TEXT,
  evaluator_provider     TEXT,
  evaluation_latency_ms  REAL NOT NULL,
  evaluation_cost_micros INTEGER,
  job_created_at         INTEGER NOT NULL,
  written_at             INTEGER NOT NULL
);
CREATE INDEX ix_evaluation_results_span ON evaluation_results (project_id, trace_id, span_id);

-- Demo-only: fixed-window request counters for per-IP / per-key limits.
CREATE TABLE rate_limits (
  key          TEXT PRIMARY KEY,
  window_start INTEGER NOT NULL,
  count        INTEGER NOT NULL
);
