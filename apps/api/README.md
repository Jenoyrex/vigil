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

For local development, mint a key against a demo project with:

```bash
uv run python scripts/seed_local_api_key.py
```

For a production deployment's first key, see "Provisioning" below instead —
`POST /v1/provisioning/bootstrap` is the HTTP equivalent of this script, protected by a
one-time bootstrap secret.

Either way, the raw key is printed/returned exactly once — e.g.
`vgl_41ce27b462d0.jeK-Mf6i9aRYOrQvm1ZbNYD3aFibJdzHcmbLEAJ592c` — and cannot be recovered
afterwards; the database only ever stores a SHA-256 hash of it (`app/security/api_keys.py`),
never the raw value.

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

## Rate limiting

Every authenticated customer endpoint is rate limited per API key (`AuthenticatedKey.api_key_id`,
never the raw key or `project_id` alone), via a small in-process token bucket
(`app/api/rate_limit.py`) -- no Redis or external store. Two tiers:

| Tier | Applies to | Default capacity (burst) | Default refill |
|---|---|---|---|
| Ingestion | `POST /v1/traces` | `VIGIL_API_RATE_LIMIT_INGESTION_CAPACITY` (20) | `VIGIL_API_RATE_LIMIT_INGESTION_REFILL_PER_SECOND` (5/s) |
| Default | Every other authenticated endpoint (`GET /v1/traces*`, `GET /v1/analytics/*`, `GET`/`PUT /v1/evaluations/*` except job creation) | `VIGIL_API_RATE_LIMIT_DEFAULT_CAPACITY` (60) | `VIGIL_API_RATE_LIMIT_DEFAULT_REFILL_PER_SECOND` (20/s) |

These are starting-point defaults, not load-tested production numbers -- there is no production
traffic history yet to calibrate against.

Not rate limited: `/health`, `/ready` (unauthenticated infrastructure endpoints), and
`POST /v1/evaluations/jobs` (internal, worker-only, authenticated by
`X-Vigil-Internal-Token` -- a single trusted caller on a fixed polling cadence has no
customer-abuse threat model to defend against).

Exceeding a limit returns `429` with `Retry-After: <N>` (whole seconds) and a body of
`{"detail": "Rate limit exceeded. Retry after N seconds."}` -- `packages/sdk-python`'s `Vigil`
client already retries this exact shape with backoff (see its own README's "Retries" section).

**In-process, not distributed.** Rate-limit state lives in this one `api` process's memory, keyed
by API key, bounded to at most `VIGIL_API_RATE_LIMIT_MAX_TRACKED_API_KEYS` concurrently-tracked
keys (least-recently-used evicted beyond that). Today's production topology
(`infrastructure/docker-compose.prod.yml`) runs exactly one `api` replica, so this is a real,
correctly-enforced limit -- but if `api` is ever horizontally scaled to multiple replicas, each
replica enforces its own independent budget (the effective limit becomes roughly
`configured_limit * replica_count`). Revisiting this design (a shared store) would be necessary at
that point; not needed today.

## CORS

Deny-by-default: `VIGIL_API_CORS_ALLOWED_ORIGINS` is empty by default, so no cross-origin browser
request ever receives an `Access-Control-Allow-Origin` header. This is a deliberate default, not a
gap -- this API's only real consumers today are `packages/sdk-python` (a non-browser HTTP client;
CORS is a browser-only enforcement mechanism and doesn't apply to it at all) and
`apps/dashboard`'s own server process, which calls this API directly server-to-server
(`apps/dashboard/lib/api/vigilClient.ts`) and never exposes it to browser JS. There is no current
consumer CORS needs to accommodate.

If a browser-based consumer is ever introduced, set `VIGIL_API_CORS_ALLOWED_ORIGINS` to a
comma-separated list of exact origins (e.g.
`https://app.example.com,https://admin.example.com`). `allow_credentials` is always `false` --
this API authenticates via `Authorization: Bearer <api-key>`, never cookies, so credentialed CORS
has no purpose here. Allowed methods/headers are scoped to exactly what this API's routes use
(`GET`/`POST`/`PUT`, `Authorization`/`Content-Type`), not wildcarded. A literal `*` in
`VIGIL_API_CORS_ALLOWED_ORIGINS` is rejected at startup (`app.config.Settings.
cors_allowed_origins_list` raises) -- wildcard CORS is not supported by this API at all, even if
manually typed into the environment.

See `docs/decisions/007-cors-and-dashboard-security-headers.md` for the full rationale.

## Provisioning

`POST /v1/provisioning/bootstrap` (Phase 4D, F3; extended in Phase 4D F1) is the production,
HTTP-based equivalent of `scripts/seed_local_api_key.py`: a one-time way to create the first
organization, project, API key, and dashboard owner user in a deployment where an operator may
not have (or want) direct database access. It is **bootstrap provisioning for a single trusted
operator, not a public signup system** — this is the *only* place a dashboard user can ever be
created; there is no separate signup endpoint, and dashboard login itself
(`POST /v1/auth/login`, see "Dashboard authentication" below) never creates one either.

### Enabling it

The endpoint is disabled by default and returns `401` for every request until an operator
explicitly opts in. Set a real, high-entropy secret **only in the specific environment you are
about to bootstrap, for as long as you need it**:

```bash
# 1. Generate a secret.
openssl rand -hex 32

# 2. Add it to infrastructure/.env.production (never commit this file):
#      VIGIL_API_BOOTSTRAP_SECRET=<generated-secret>
# 3. Recreate just the api container so it picks up the new value:
docker compose -f infrastructure/docker-compose.prod.yml \
  --env-file infrastructure/.env.production up -d api

# 4. Call the endpoint (see "Calling it" below).

# 5. Remove VIGIL_API_BOOTSTRAP_SECRET from infrastructure/.env.production again,
#    then recreate api once more so bootstrap goes back to disabled:
docker compose -f infrastructure/docker-compose.prod.yml \
  --env-file infrastructure/.env.production up -d api
```

Never commit a real value — `apps/api/.env.example`'s own `VIGIL_API_BOOTSTRAP_SECRET=` line is
deliberately empty and must stay that way.

### Calling it

```bash
curl -X POST http://127.0.0.1:8000/v1/provisioning/bootstrap \
  -H "X-Vigil-Bootstrap-Token: <the secret from above>" \
  -H "Content-Type: application/json" \
  -d '{
    "organization_name": "Acme Corp",
    "organization_slug": "acme-corp",
    "project_name": "Production",
    "project_slug": "production",
    "api_key_name": "Production key",
    "owner_email": "you@acme.example",
    "owner_full_name": "Ada Lovelace",
    "owner_password": "<a real password, at least 12 characters>"
  }'
```

On success (`201`), the response contains `organization_id`, `project_id`, `api_key_id`,
`api_key`, `user_id`, and `owner_email`. `api_key` is the plaintext key, **shown exactly once and
never retrievable again** (only its SHA-256 hash is ever persisted, identically to
`scripts/seed_local_api_key.py`'s own key issuance) — copy it immediately.
`owner_password` is **not** included anywhere in the response (you already know it, having just
chosen it) — only its `scrypt` hash is persisted, on the new `users` row. Log into the dashboard
with `owner_email`/`owner_password` via its `/login` page (`POST /v1/auth/login` underneath) —
see "Dashboard authentication" below.

### Repeated calls

Bootstrap succeeds **at most once per deployment** — a second call, whether sent later or
concurrently with the first, always receives `409 Conflict` and creates nothing. This is enforced
by the database in two layers, not merely an application-level check: (1) `organizations` being
non-empty is checked first, before anything else, and alone refuses any repeat call; (2) an
atomic, conflict-checked insert against a dedicated single-row table (`provisioning_bootstrap`)
is what makes a *successful* run safe even when two bootstrap requests genuinely race with each
other. See `app/services/provisioning.py`'s module docstring for the full argument.

**There is no "just delete a row" way to re-enable bootstrap, and none should ever be
documented as one.** `provisioning_bootstrap` is an audit record, not the only gate — deleting
only that row does **not** delete the organization/project/API key it points to, so bootstrap
would still refuse (layer (1) above still sees the organization). Re-enabling bootstrap requires
a full, destructive teardown of everything the original run created:

```sql
-- 1. Find the organization bootstrap created, and its owner user.
SELECT organization_id FROM provisioning_bootstrap WHERE id = 'bootstrap';
SELECT user_id FROM organization_memberships WHERE organization_id = '<org-id>';

-- 2. Substitute those UUIDs for <org-id>/<user-id> below and run as ONE
--    transaction, in exactly this order (api_keys/projects reference
--    organizations with ondelete=RESTRICT, so this order is required, not
--    just convention).
--
--    DESTRUCTIVE. Irrecoverably deletes the organization, its project(s),
--    its API key(s), its owner user, and that user's dashboard
--    sessions/memberships. This is an operator-level, disposable
--    development/staging procedure -- NEVER run this against a production
--    deployment holding real data, and never expose it through any API.
BEGIN;
DELETE FROM api_keys WHERE project_id IN (SELECT id FROM projects WHERE organization_id = '<org-id>');
DELETE FROM projects WHERE organization_id = '<org-id>';
DELETE FROM provisioning_bootstrap WHERE id = 'bootstrap';
DELETE FROM organization_memberships WHERE organization_id = '<org-id>';
DELETE FROM organizations WHERE id = '<org-id>';
DELETE FROM dashboard_sessions WHERE user_id = '<user-id>';
DELETE FROM users WHERE id = '<user-id>';
COMMIT;
```

After this, `organizations` is empty again (assuming it held only the bootstrapped org) and
`POST /v1/provisioning/bootstrap` will succeed again. Note that the up-front `organizations`
check also refuses bootstrap if *any other* organization exists in this database for any reason
(e.g. one created by `scripts/seed_local_api_key.py`) — a full clean slate means that table is
empty, not just that this one organization is gone.

### Rotating or revoking the resulting key

There is no HTTP endpoint for this yet — use direct database access, the same way
`scripts/seed_local_api_key.py`-issued keys are managed today:

```sql
UPDATE api_keys SET status = 'revoked', revoked_at = now() WHERE id = '<api_key_id>';
```

Minting a replacement key today also means direct database access (insert a new `api_keys` row
via `scripts/seed_local_api_key.py`'s pattern, or `psql`) — an HTTP key-management endpoint is
tracked as a known gap, not solved by this phase.

### Security limitations, explicitly

This is a minimal bootstrap mechanism, not a complete provisioning/identity system:

- It authenticates via a single shared secret (`X-Vigil-Bootstrap-Token`, compared with
  `hmac.compare_digest`), not per-operator credentials — anyone with the secret can bootstrap.
- It rate-limits by client IP (`VIGIL_API_BOOTSTRAP_RATE_LIMIT_*`, in-process, no Redis) to slow
  brute-forcing the secret, not to prevent it outright — use a real, high-entropy secret.
- It creates exactly one dashboard user, as the organization's `owner` — there is no invite/signup
  flow to add a second one yet; see "Dashboard authentication" below for what login itself
  supports today.
- It never accepts a customer `vgl_*` API key as authorization, and a customer key can never
  provision additional organizations/projects/keys through it.

## Dashboard authentication

`POST /v1/auth/login`, `POST /v1/auth/logout`, and `GET /v1/auth/session` (Phase 4D, F1)
authenticate a human logging into `apps/dashboard`, entirely separate from customer API-key
authentication above — a `vgl_*` key satisfies neither of these endpoints, and login credentials
satisfy neither of the `vgl_*`-key-protected endpoints. There is no signup endpoint; the only user
ever created is bootstrap's owner user (see "Provisioning" above).

- **`POST /v1/auth/login`** — `{"email": ..., "password": ...}`. On success (`200`), returns
  `{"session_token": ..., "expires_at": ...}` — the raw session token, shown exactly once (only
  its SHA-256 hash is persisted, on `dashboard_sessions`). On any failure — unknown email, wrong
  password, an inactive account, or a user with no organization membership — returns the
  identical generic `401 {"detail": "Invalid email or password."}`, deliberately never revealing
  which. Rate-limited by client IP (`VIGIL_API_LOGIN_RATE_LIMIT_*`, in-process, no Redis — the
  same `RateLimiter` primitive bootstrap uses, a separate, more generous tier).
- **`GET /v1/auth/session`** — presents the raw token via the `X-Vigil-Session-Token` header
  (never `Authorization: Bearer`, never a query parameter). Returns `200` with the session's
  owning user if the token is valid, unexpired, unrevoked, and its user is still active; a
  generic `401 {"detail": "Invalid or expired session."}` otherwise. `apps/dashboard`'s own proxy
  calls this on every request to a gated route — there is no caching layer in front of it on
  either side, so a revoked session is rejected on its very next request.
- **`POST /v1/auth/logout`** — same `X-Vigil-Session-Token` header. Revokes the matching session
  (sets `revoked_at`) and always returns `204`, even for a missing/unknown/already-revoked token
  — idempotent by design, so a client never needs special handling for "log out when maybe
  already logged out."

Session lifetime defaults to 12 hours (`VIGIL_API_DASHBOARD_SESSION_TTL_HOURS`), with no sliding
renewal in this phase — a session simply expires at `expires_at` regardless of activity, and the
dashboard redirects to `/login` again.

Password hashing uses `hashlib.scrypt` (Python stdlib, RFC 7914's interactive-login parameters)
— never the fast SHA-256 this codebase uses for API keys/session tokens, which are
server-generated high-entropy secrets, not human-chosen ones. See
`app/security/passwords.py`/`app/security/sessions.py`.

## Logging

Structured (JSON Lines) logging on stdout -- one JSON object per line, every field a genuine
top-level key (`timestamp`, `level`, `service`, `logger`, `message`, plus request-scoped fields
like `request_id`/`project_id` where relevant) rather than text embedded in a message string. See
`app/logging_config.py`'s module docstring for the full design.

`VIGIL_API_LOG_LEVEL` (default `INFO`) controls the root logger level; one of
`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` -- anything else fails at startup.

Every HTTP request gets a `request_id` (`app.middleware.RequestIdMiddleware`, always minted
server-side, never trusted from a client-supplied header), bound for the lifetime of that request
so every log line emitted anywhere in its call stack carries it automatically, and echoed back as
an `X-Request-Id` response header. `POST /v1/traces`'s own `request_id` response field is this
same value.

**Local development sees the identical JSON output production does** -- deliberately not a second,
prettier console formatter, so there is only one code path to verify. Pipe through `jq` for a
readable view:

```bash
uv run uvicorn app.main:app --reload | jq .
```

**Never logged**: API keys, bearer tokens, the internal service token, database URLs, or raw span
input/output/attributes. Every call site's `extra={...}` is limited to identifiers (request/
project/trace/span ids, evaluator name/version, counts, error type names) -- see
`app/logging_config.py`'s security note.

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
