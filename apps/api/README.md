# Vigil API

`apps/api` is the FastAPI backend for Vigil. It is an independent Python project managed with
[uv](https://github.com/astral-sh/uv); it does not share a workspace or dependency lockfile with
any other part of the monorepo.

It exposes a health check endpoint, the PostgreSQL schema (users, organizations, memberships,
projects, API keys) via SQLAlchemy + Alembic, telemetry ingestion (`POST /v1/traces`), and the
read side of the Trace Explorer/analytics API (`GET /v1/traces*`, `GET /v1/analytics/*`) --
authenticated via the same API key, writing to and reading from ClickHouse respectively. No
background jobs, SDK-facing dashboard, or evaluator have been added yet.

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [uv](https://github.com/astral-sh/uv)
- Docker (for local PostgreSQL and ClickHouse, via `infrastructure/docker-compose.yml`)

## Install dependencies

From `apps/api`:

```bash
uv sync
```

This creates a local `.venv` and installs both runtime and development dependencies.

## Database setup

Start a local PostgreSQL instance from `infrastructure`:

```bash
cd infrastructure
docker compose up -d postgres
```

This starts Postgres on `localhost:5434` (chosen to avoid colliding with any other local Postgres
on the default `5432`) with a `vigil` database for development and a `vigil_test` database for
tests, both created automatically. Override credentials/port via `infrastructure/.env` (see
`infrastructure/.env.example`) rather than editing `docker-compose.yml`.

Copy `apps/api/.env.example` to `apps/api/.env` and adjust if you changed the defaults.

Apply migrations (from `apps/api`):

```bash
uv run alembic upgrade head
```

This must be run against both the `vigil` database (used by the app) and the `vigil_test`
database (used by the test suite) — point `VIGIL_API_DATABASE_URL` at each in turn, or use
`VIGIL_API_DATABASE_URL=<vigil_test URL> uv run alembic upgrade head` for the second one.

### Creating new migrations

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Always review generated migrations before applying them.

### updated_at strategy

`updated_at` is maintained by SQLAlchemy's `onupdate=func.now()` (see `app/db/base.py`), not a
PostgreSQL trigger: the ORM includes `now()` in the `UPDATE` statement it issues, so the timestamp
is still computed by the database (avoiding client clock skew) while the decision to update it
stays at the application layer. This only refreshes `updated_at` for writes made through
SQLAlchemy — a documented, accepted limitation while all writes go through the ORM.

## ClickHouse setup

Start a local ClickHouse instance from `infrastructure` (see
`infrastructure/clickhouse/README.md` for the full details):

```bash
cd infrastructure
docker compose up -d clickhouse
```

This starts ClickHouse on `localhost:8123` (HTTP) / `localhost:9000` (native), with the `spans`
table created automatically from `infrastructure/clickhouse/init/`. No migration step is needed —
unlike Postgres/Alembic, ClickHouse schema changes here are deployed by editing that init SQL and
recreating the container against a fresh volume; there is no ClickHouse equivalent of `alembic
upgrade head` in this project yet.

Configure the connection via `VIGIL_API_CLICKHOUSE_*` environment variables (same `.env` file as
the Postgres settings, see `apps/api/.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `VIGIL_API_CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `VIGIL_API_CLICKHOUSE_PORT` | `8123` | HTTP port (the client only speaks HTTP) |
| `VIGIL_API_CLICKHOUSE_DATABASE` | `vigil` | Database containing `spans` |
| `VIGIL_API_CLICKHOUSE_USER` | `vigil` | Matches `infrastructure/.env.example` |
| `VIGIL_API_CLICKHOUSE_PASSWORD` | `vigil` | Local dev only — never a real credential |
| `VIGIL_API_CLICKHOUSE_TIMEOUT_SECONDS` | `10.0` | Connect and read/write timeout |

These live in the same `app/config.py` `Settings` object as everything else (`env_prefix =
"VIGIL_API_"`) — there is one configuration system for the whole app, not a separate one per
datastore.

## Run the API locally

From `apps/api`:

```bash
uv run uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`. Check the health and readiness endpoints:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl http://127.0.0.1:8000/ready
# {"status":"ok","clickhouse":"ok"}   (or 503 if ClickHouse is unreachable)
```

`/health` is a pure liveness check and never depends on ClickHouse or Postgres being reachable;
`/ready` checks ClickHouse connectivity and is the one that can fail.

## Telemetry ingestion (`POST /v1/traces`)

### API-key authentication

There is no key-issuance HTTP endpoint yet. For local development, mint a key against a demo
project with:

```bash
uv run python scripts/seed_local_api_key.py
```

This prints a raw key exactly once — e.g. `vgl_41ce27b462d0.jeK-Mf6i9aRYOrQvm1ZbNYD3aFibJdzHcmbLEAJ592c`
— and cannot be recovered afterwards; the database only ever stores a SHA-256 hash of it
(`app/security/api_keys.py`), never the raw value.

Send it as `Authorization: Bearer <api-key>`. On each request the API:

1. Extracts the bearer token and cheaply rejects anything that doesn't look like a Vigil key
   (`vgl_<prefix>.<secret>`) before doing any hashing or database work.
2. Hashes the presented key and looks it up by `key_hash` (already uniquely indexed on
   `api_keys`).
3. Rejects an unknown or non-`active` (revoked) key with `401`.
4. Resolves `project_id` from the matched row and updates `last_used_at`.

`project_id` is **never** accepted from the request body — it is always the value resolved in
step 4, so one project can never inject spans into another's data by guessing/setting a
`project_id` field.

### Example request

Using `examples/telemetry/sample-trace-request.json` (repo-relative from `apps/api`:
`../../examples/telemetry/sample-trace-request.json`):

```bash
curl -X POST http://127.0.0.1:8000/v1/traces \
  -H "Authorization: Bearer $VIGIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d @../../examples/telemetry/sample-trace-request.json
```

### Example response

```json
{ "accepted": 1, "request_id": "eac7fe6d-6f6b-4c31-b4a3-32711748b0b3" }
```

`accepted` is the number of spans stored; `request_id` is only for correlating with server logs —
it is not a delivery receipt or idempotency token (see below). The endpoint returns plain `200 OK`,
not `202 Accepted`: insertion into ClickHouse happens synchronously, inside the request/response
cycle, with no queue or background worker in this design, so by the time the response is sent the
batch has actually been written (or the request has failed with a `4xx`/`5xx`). `202` is reserved
for genuinely deferred/asynchronous processing, which doesn't exist here.

### Payload limits

Per `docs/decisions/003-clickhouse-telemetry-storage.md`:

- `input` and `output` are each truncated at **64 KiB** (UTF-8 bytes, cut on a valid character
  boundary) rather than rejected. Truncation is never silent: `input_truncated`/`output_truncated`
  and `input_size_bytes`/`output_size_bytes` (the pre-truncation size) are recorded on the stored
  row.
- The whole span is additionally bounded at **256 KiB** total. `attributes`, then `events`, are
  trimmed (whole entries dropped from the tail, in the order given) to fit whatever budget remains
  after `input`/`output`, with `attributes_truncated`/`events_truncated` set if that happened.
- A request may contain at most `VIGIL_API_MAX_SPANS_PER_REQUEST` spans (default 1000) — more
  returns `422`.
- The request body itself is capped at `VIGIL_API_MAX_REQUEST_BODY_BYTES` (default 10 MiB) — over
  that returns `413`. This is enforced from the `Content-Length` header before the body is parsed;
  a request using chunked transfer encoding (no `Content-Length`) skips that check and is bounded
  only by the per-request span-count limit instead (see `app/middleware.py`).

### Duplicate requests / idempotency

The logical identity of a span is `(project_id, trace_id, span_id)`. Retrying an identical request
is safe, but **this API does not provide exactly-once delivery**:

- ClickHouse's `ReplacingMergeTree` on `spans` eventually collapses duplicate-identity rows during
  background merges — not on insert.
- Immediately after a retry, a plain query can see both the original and the retried row. Reads
  that need immediate correctness must use `FINAL` (or `LIMIT 1 BY (project_id, trace_id,
  span_id)`), same as any other ClickHouse reader.
- There is no API-level idempotency key or dedup cache in front of ClickHouse. That is
  intentionally out of scope for this stage (see
  `docs/decisions/003-clickhouse-telemetry-storage.md` decision 8) and would be its own follow-up
  ADR if added.

## Trace Explorer & analytics (read API)

Five read-only `GET` endpoints, all authenticated the same way as ingestion
(`Authorization: Bearer <api-key>`) and scoped exclusively to the project resolved from that key
-- there is no `project_id` parameter on any of them, so there is nothing a client could supply to
override tenant scoping even by mistake.

| Endpoint | Purpose |
|---|---|
| `GET /v1/traces` | List traces (derived from spans), most recent first |
| `GET /v1/traces/{trace_id}` | One trace and its spans |
| `GET /v1/traces/{trace_id}/spans/{span_id}` | One span, in full |
| `GET /v1/analytics/spans` | Span count, error rate, latency percentiles |
| `GET /v1/analytics/llm-usage` | LLM token usage and cost |

### Time windows

`GET /v1/traces`, `GET /v1/analytics/spans`, and `GET /v1/analytics/llm-usage` all take optional
`start_time_from`/`start_time_to` query parameters (RFC3339, and **must** include a UTC offset --
a naive timestamp is rejected with `422`):

- Omitting both defaults to the previous `VIGIL_API_DEFAULT_QUERY_WINDOW_HOURS` (24h).
- The window may not exceed `VIGIL_API_MAX_QUERY_WINDOW_DAYS` (7 days) -- wider windows are
  rejected with `422`, so a query can never accidentally scan the full 30-day retention window.
- `start_time_from` after `start_time_to` is also a `422`.

### Trace list pagination

`GET /v1/traces` is ordered **`start_time DESC, trace_id DESC`** -- `trace_id` is a deterministic
tie-breaker, not an incidental detail, since it's what keeps keyset pagination stable when
multiple traces share the same `start_time`. Pagination is cursor-based (`next_cursor` in the
response, `cursor` query parameter on the next request), never `OFFSET`: an opaque, base64-encoded
token that encodes the previous page's last `(start_time, trace_id)`. This avoids both problems
`OFFSET` has against continuously-ingested data -- unbounded per-page cost, and pages that skip or
duplicate rows as new spans land while a user is paging through results.

### Trace status

A trace's `status` (`ok`/`error`/`unknown`) is derived from its spans, per
`docs/decisions/002-trace-span-telemetry-model.md` decision 6: `error` if any span has
`status = error`; else `ok` once the root span (`parent_span_id IS NULL`) has arrived; else
`unknown`.

### `FINAL` usage

`GET /v1/traces/{trace_id}` and `GET /v1/traces/{trace_id}/spans/{span_id}` query ClickHouse with
`FINAL` -- immediate deduplication, since these are the single-trace/single-span detail views
`docs/decisions/003-clickhouse-telemetry-storage.md` section 8 identifies as needing it, and the
query is already narrowed to one `project_id` + `trace_id` (+ `span_id`), so the cost is bounded.
`GET /v1/traces` and both `/v1/analytics/*` endpoints deliberately do **not** use `FINAL` -- they
are broad aggregates that tolerate `ReplacingMergeTree`'s eventual deduplication (the same section
8 explicitly allows this), and forcing `FINAL` there would mean merge-level dedup logic running
over a much larger scanned range on every request.

### Traces with many spans

`GET /v1/traces/{trace_id}` caps the returned `spans` array at
`VIGIL_API_MAX_SPANS_PER_TRACE_RESPONSE` (default 2000). If a trace has more spans than that,
`truncated: true` is set and `total_span_count` reports the real total (from a separate, cheap,
untruncated aggregate query -- not the possibly-truncated page itself, since a trace's `status`
must stay correct even for a truncated trace). There is no pagination of spans *within* one trace
in V1.

### Analytics `group_by`/`bucket`

`GET /v1/analytics/spans` supports `group_by` (`environment` | `span_type` | `release` |
`resource`) or `bucket` (`hour` | `day`) -- mutually exclusive (`422` if both are set). Grouped
results are capped at the top 50 (by `span_count`, descending); bucketed results are chronological
and implicitly bounded by the 7-day window cap. `GET /v1/analytics/llm-usage` only counts spans
with a non-null `llm_provider` -- the documented "this is an LLM span" signal, independent of
`span_type` -- and supports `group_by` of `llm_provider` | `llm_model` | `environment`, capped at
the top 50 by `total_cost_usd`. `total_cost_usd` is a JSON **string**, not a number, to preserve
`Decimal64(6)` precision.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VIGIL_API_MAX_QUERY_WINDOW_DAYS` | `7` | Max `start_time_from`/`start_time_to` span for list/analytics endpoints |
| `VIGIL_API_DEFAULT_QUERY_WINDOW_HOURS` | `24` | Window used when both bounds are omitted |
| `VIGIL_API_MAX_SPANS_PER_TRACE_RESPONSE` | `2000` | Cap on spans returned by trace detail |

## Run tests

Requires the `vigil_test` database (see Database setup above) with migrations applied. From
`apps/api`:

```bash
uv run pytest
```

Tests run against `VIGIL_API_TEST_DATABASE_URL` (defaults to the local `vigil_test` database) and
never touch the `vigil` development database. `tests/conftest.py` refuses to run if that URL
doesn't point at a database whose name ends in `test`, and truncates all tables before and after
each test for isolation.

Most tests use a fake ClickHouse repository -- `tests/conftest.py`'s `fake_repository` (ingestion),
`fake_traces_query_repository` (list/detail/span), and `fake_analytics_repository` (analytics)
fixtures, all wired into the shared `client` TestClient fixture -- and never need a real ClickHouse
server:

- `tests/test_traces_*.py`, `tests/test_spans_detail.py`, `tests/test_analytics_*.py`: route-level
  tests (auth, validation, tenant scoping, response shape) against the fake repositories.
- `tests/test_query_repository.py`, `tests/test_analytics_repository.py`: repository-level tests
  using `fake_ch_query_client` (a fake `clickhouse_connect` client) to assert the *exact* generated
  SQL and bound parameters -- tenant scoping, `FINAL` presence/absence, no `OFFSET`, etc.
- `tests/test_query_service.py`, `tests/test_analytics_service.py`: pure unit tests for time-window
  validation, cursor encode/decode, status derivation, and NaN/Decimal handling.

`tests/test_traces_clickhouse_integration.py` and `tests/test_query_clickhouse_integration.py` are
the exceptions — they run against a real local ClickHouse (ingest via `POST /v1/traces`, then read
back through every `GET` endpoint, including a tenant-isolation check across two projects) and skip
themselves automatically (with a message explaining why) if one isn't reachable, so the rest of the
suite isn't blocked by them.

## Run Ruff

From `apps/api`:

```bash
uv run ruff check .
```

`alembic/versions/` is excluded from linting since those files are Alembic-generated.
