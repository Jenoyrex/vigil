# Vigil

Vigil is an LLM observability and evaluation platform: an SDK sends trace/span telemetry from an
LLM application to a FastAPI ingestion API, which stores it in ClickHouse; a dashboard lets you
explore that telemetry (traces, spans, analytics) and configure automated evaluators; a background
worker/poller pipeline runs those evaluators against sampled spans and writes results back
alongside the telemetry they scored.

This README describes the system as it exists in this repository today. See
[`docs/decisions/`](./docs/decisions) for the architecture decision records (ADRs) explaining *why*
it's built this way, and each component's own README (linked below) for full operational detail.

## Repository layout

```
apps/
  api/                 FastAPI backend: ingestion, Trace Explorer/analytics read API,
                        dashboard auth, provisioning. See apps/api/README.md.
  dashboard/            Next.js + TypeScript dashboard (traces, analytics, evaluations, login).

packages/
  sdk-python/           Python SDK for sending telemetry to the ingestion API.
                        See packages/sdk-python/README.md.

services/
  worker/               Claims and executes evaluation jobs against ClickHouse-stored spans,
                        plus the poller that creates those jobs. See services/worker/README.md.
  evaluator/             Evaluation algorithms (relevance, embedding-based relevance) as a
                        standalone library with no storage/HTTP/queue dependencies of its own.
                        See services/evaluator/README.md.

infrastructure/          Docker Compose for local dev and production, ClickHouse schema/init,
                        Postgres init, and backup/restore scripts. See "Deployment" below.

docs/
  decisions/             Architecture decision records (ADRs 001-008).

examples/
  python-sdk/            A runnable end-to-end example using packages/sdk-python.
  telemetry/              A sample raw POST /v1/traces request body.
```

There is currently **one** SDK (`packages/sdk-python`). A TypeScript SDK does not exist yet — see
"Current limitations" below.

## Tooling

- **TypeScript** (`apps/dashboard`): a [pnpm workspace](./pnpm-workspace.yaml). No other JS
  monorepo tool (npm workspaces, Turborepo, Nx) is used.
- **Python** (`apps/api`, `packages/sdk-python`, `services/worker`, `services/evaluator`): each is
  an independent project managed with [uv](https://github.com/astral-sh/uv), its own
  `pyproject.toml`, and its own lockfile — none of them share a workspace or import from one
  another except `services/worker`, which imports `services/evaluator` directly (see ADR 004
  section 4, ADR 005 section 6).

See [`docs/decisions/001-system-architecture.md`](./docs/decisions/001-system-architecture.md) for
the reasoning behind these choices.

## Components

### API (`apps/api`)

The FastAPI backend, and the only thing every other component talks to — nothing else in this
repository accesses PostgreSQL or ClickHouse directly except `apps/api` and `services/worker`
(worker/poller access their own scoped tables directly, per ADR 005, without going through the
API).

- **Telemetry ingestion** — `POST /v1/traces`, authenticated by a customer API key
  (`Authorization: Bearer vgl_<prefix>.<secret>`), writes to ClickHouse synchronously.
- **Trace Explorer / analytics (read API)** — `GET /v1/traces*`, `GET /v1/analytics/*`, scoped
  exclusively to the authenticated key's project.
- **Evaluation config/query API** — `GET`/`PUT /v1/evaluations/configs*`,
  `GET /v1/evaluations/jobs`, `GET /v1/traces/{trace_id}/spans/{span_id}/evaluations`, plus an
  internal, worker-only `POST /v1/evaluations/jobs` (authenticated by a separate shared
  `X-Vigil-Internal-Token`, not a customer key).
- **Dashboard authentication** — `POST /v1/auth/login`, `POST /v1/auth/logout`,
  `GET /v1/auth/session`, entirely separate from customer API-key auth.
- **Provisioning** — `POST /v1/provisioning/bootstrap`, a one-time, secret-gated way to create the
  first organization/project/API key/dashboard owner without direct database access.
- **Rate limiting** — an in-process, per-API-key token bucket (`app/api/rate_limit.py`); separate
  tiers for ingestion, every other authenticated customer endpoint, login, and bootstrap.

Full detail — including payload limits, idempotency semantics, pagination, and every configuration
variable — is in [`apps/api/README.md`](./apps/api/README.md).

### Dashboard (`apps/dashboard`)

A Next.js app: a login page backed by dashboard-session auth, a trace list/detail view, an
analytics view, and an evaluations view (job list + per-evaluator configuration). It never talks
to the ingestion API's write path or holds a customer API key in the browser — the dashboard's own
server process calls `apps/api` directly (`lib/api/vigilClient.ts`, server-only, never bundled to
the client), and `proxy.ts` re-validates the dashboard session on every gated request.

### Python SDK (`packages/sdk-python`)

The client library applications use to send telemetry: `vigil.start_span(...)` context managers
that buffer completed spans in memory and flush them to `POST /v1/traces` on a background thread,
with retry/backoff on transient failures (honoring `Retry-After`) and no-op-on-failure semantics
everywhere except an explicit `flush()` call, so a broken backend never breaks the instrumented
application. Full detail in [`packages/sdk-python/README.md`](./packages/sdk-python/README.md).

### Evaluation engine (`services/evaluator`)

A standalone library of evaluators (currently `RelevanceEvaluator` and
`EmbeddingRelevanceEvaluator`), each matching the `Evaluator` protocol
(`name`, `version`, `evaluate(input) -> EvaluationResult`). It has no dependency on storage, HTTP,
or a job queue — `services/worker` is what actually calls it in production. See
[`services/evaluator/README.md`](./services/evaluator/README.md) and
[`docs/decisions/004-evaluation-engine.md`](./docs/decisions/004-evaluation-engine.md).

### Worker + poller (`services/worker`)

Two processes built from the same package/image (`services/worker/Dockerfile`), distinguished by
their entrypoint:

- **`poller`** — periodically finds eligible spans and creates evaluation jobs for them via
  `apps/api`'s internal job-creation endpoint.
- **`worker`** — claims pending jobs from PostgreSQL (`SELECT ... FOR UPDATE SKIP LOCKED`), runs
  them through `services/evaluator` with bounded concurrency, writes results to ClickHouse's
  `evaluation_results` table, and marks the PostgreSQL job row succeeded/failed/dead-lettered —
  with retry/backoff for transient failures, immediate dead-lettering for permanent ones, and a
  reaper that reclaims jobs left `running` by a process that died mid-execution.

Both processes write a liveness heartbeat file, checked by their Docker `HEALTHCHECK`. See
[`services/worker/README.md`](./services/worker/README.md) and
[`docs/decisions/005-evaluation-job-storage-worker.md`](./docs/decisions/005-evaluation-job-storage-worker.md).

### PostgreSQL

The control-plane database: organizations, projects, API keys, dashboard users/sessions,
provisioning-bootstrap state, and evaluation job/config rows. Owned and migrated by `apps/api` via
Alembic (`apps/api/alembic/`) — `services/worker` talks to the same schema over raw `psycopg`
(never SQLAlchemy, never importing `apps/api`'s ORM) but does not migrate it.

### ClickHouse

The telemetry store: `spans` (one row per span, `ReplacingMergeTree`, 30-day TTL) and
`evaluation_results` (one row per evaluation, joined back to `evaluation_id = evaluation_jobs.id`).
Schema lives in `infrastructure/clickhouse/init/*.sql`, applied automatically on first container
start in local dev, and as an explicit CI step (no ClickHouse equivalent of `alembic upgrade head`
exists yet — a schema change means editing that SQL and recreating against a fresh volume). See
[`docs/decisions/003-clickhouse-telemetry-storage.md`](./docs/decisions/003-clickhouse-telemetry-storage.md).

## Authentication

Two entirely separate authentication systems, neither of which satisfies the other:

- **Customer API keys** (`vgl_<prefix>.<secret>`) — `Authorization: Bearer <key>`, for telemetry
  ingestion and the read API. Only a SHA-256 hash is ever stored; the raw key is shown exactly once
  at creation time.
- **Dashboard sessions** — email/password login (`hashlib.scrypt` hashing), a server-generated
  session token (`X-Vigil-Session-Token`, SHA-256 hashed at rest, HttpOnly/Secure/SameSite=Strict
  cookie on the dashboard side), re-validated on every gated dashboard request, 12-hour default
  lifetime, no signup endpoint — the only dashboard user ever created is provisioning's owner user.

Full detail in `apps/api/README.md`'s "Provisioning" and "Dashboard authentication" sections.

## Provisioning

`POST /v1/provisioning/bootstrap` is the production, HTTP-based way to create the first
organization/project/API key/dashboard owner in a deployment without direct database access.
Disabled by default (`401` for every request) until an operator sets a real, high-entropy
`VIGIL_API_BOOTSTRAP_SECRET`; succeeds at most once per deployment, enforced at the database level
(not just an application check) so two concurrent bootstrap requests can never both succeed. See
`apps/api/README.md`'s "Provisioning" section for the full flow, including how to re-enable it
(a deliberately destructive, documented procedure) and its explicit security limitations.

## Deployment

`infrastructure/docker-compose.prod.yml` runs the full stack: `postgres`, `clickhouse`, a one-shot
`migrate` service (Alembic), `api`, `dashboard`, `worker`, and `poller`. Configuration is entirely
environment-variable driven, via `infrastructure/.env.production` (copy from
`infrastructure/.env.production.example` and fill in real values — **never commit the filled-in
file**; every value in the example file is a placeholder, and the example file itself is the only
`.env.*` variant `.gitignore` allows into git).

```bash
docker compose -f infrastructure/docker-compose.prod.yml \
  --env-file infrastructure/.env.production up -d --build
```

Images are normally pulled from a registry (`ghcr.io/<owner>/vigil-{api,dashboard,worker}`) rather
than built locally — see "CI/CD" below. Known, currently-deliberate scope limits of this
deployment (no TLS termination, no ClickHouse replication, in-process-only rate limiting) are
documented in [`docs/decisions/006-deployment-architecture.md`](./docs/decisions/006-deployment-architecture.md).

## CI/CD

Two GitHub Actions workflows:

- **[`.github/workflows/ci.yml`](./.github/workflows/ci.yml)** — runs on every push/PR: Ruff +
  pytest for `apps/api`, `services/worker`, `services/evaluator`, and `packages/sdk-python` (each
  against real, ephemeral PostgreSQL and ClickHouse service containers), lint/test/build for
  `apps/dashboard`, and a build-only validation of every production Dockerfile.
- **[`.github/workflows/cd.yml`](./.github/workflows/cd.yml)** — on push to `master`, waits for
  `ci.yml` to pass on that exact commit, then builds and publishes all three images to GHCR tagged
  with the immutable `sha-<commit>` (plus branch name, and `latest` on `master`). Deploying one of
  those tags to production is a separate, manual `workflow_dispatch` step (never automatic on
  push), gated behind a GitHub Environment named `production` and deploying over SSH to a single
  host running the Compose stack above, with a documented rollback (redeploy an earlier
  `sha-<commit>` tag, no rebuild). See ADR 006 decision 13 for the full design rationale, including
  why the CI-gate is a real cross-workflow poll rather than a `needs:` dependency (which can't span
  separate workflow files).

## Backup/restore

`infrastructure/backup/`:

- `pg_backup.sh` — logical `pg_dump -Fc` backup of the control-plane database, run inside the
  already-running `postgres` container.
- `clickhouse_backup.sh` — `FORMAT Native` export of `spans` and `evaluation_results`, run inside
  the already-running `clickhouse` container (ClickHouse's native `BACKUP`/`RESTORE` SQL isn't
  usable against this deployment's config, so this format was chosen instead — see ADR 008).
- `restore_drill.sh` — restores both backups into a scratch environment to verify they're actually
  usable, not just that the backup commands exited `0`.

Neither backup script is currently invoked on a schedule by anything in this repository — running
them (and copying the results off-host) is an operator responsibility today. See
[`docs/decisions/008-backup-restore.md`](./docs/decisions/008-backup-restore.md) for the full
strategy and its explicitly-acknowledged open items (scheduling, off-host copy, encryption at
rest).

## Development setup

Each component is built/run/tested independently — there is no single root-level command that sets
up everything at once. Start with the component's own README:

1. **Local datastores** — `cd infrastructure && docker compose up -d postgres clickhouse` (see
   `apps/api/README.md`'s "Database setup"/"ClickHouse setup" for ports, credentials, and how to
   override them via `infrastructure/.env.example`).
2. **API** — `apps/api/README.md`: install with `uv sync`, apply Alembic migrations, run with
   `uv run uvicorn app.main:app --reload`, mint a local API key with
   `uv run python scripts/seed_local_api_key.py`.
3. **Dashboard** — `apps/dashboard/README.md` for its own dev-server instructions; point it at the
   local API via `VIGIL_API_BASE_URL`/`VIGIL_API_KEY`.
4. **Worker/poller** — `services/worker/README.md`.
5. **Python SDK** — `packages/sdk-python/README.md`; see `examples/python-sdk/basic.py` for a
   runnable end-to-end example against a local API.

## Testing

Every component has its own test suite, run from that component's own directory:

| Component | Command | Notes |
|---|---|---|
| `apps/api` | `uv run pytest` | Needs a local `vigil_test` PostgreSQL database with migrations applied. Some tests run against real ClickHouse and self-skip if it's unreachable. |
| `services/worker` | `uv run pytest` (from `services/worker`) | Same PostgreSQL/ClickHouse dependency; integration tests self-skip if either is unreachable. |
| `services/evaluator` | `uv run pytest` | No external dependencies. |
| `packages/sdk-python` | `uv run pytest` | Mostly fakes `httpx`; one integration test self-skips unless a real local API + `VIGIL_SDK_INTEGRATION_API_KEY` are available. |
| `apps/dashboard` | see `apps/dashboard/README.md` | Vitest-based component/route tests. |

CI (`ci.yml`) runs all of the above against real, ephemeral PostgreSQL and ClickHouse containers —
see "CI/CD" above.

## Environment / configuration

Every component is configured entirely through environment variables — there is no shared,
repo-wide configuration file. Reference `*.env.example` files for variable names and safe local
defaults; **never** treat them as a source of real secrets, and never commit a filled-in
`.env`/`.env.production` file (`.gitignore` already excludes every `.env`/`.env.*` file except the
`*.example` templates):

- `apps/api/.env.example` — `VIGIL_API_*` (database, ClickHouse, rate limiting, sessions,
  bootstrap, CORS, logging).
- `services/worker/.env.example` — `VIGIL_WORKER_*` (shared by both `worker` and `poller`).
- `infrastructure/.env.example` — local-dev Postgres/ClickHouse container credentials.
- `infrastructure/.env.production.example` — the full production variable set for
  `docker-compose.prod.yml`, including which variables the two internal services must share
  identically (e.g. `VIGIL_API_INTERNAL_SERVICE_TOKEN` == `VIGIL_WORKER_INTERNAL_SERVICE_TOKEN`).

## Current limitations / deliberate deferrals

These are documented, intentional scope decisions as of this phase, not oversights:

- **No TypeScript SDK.** Only `packages/sdk-python` exists today.
- **No TLS termination or reverse proxy** in `docker-compose.prod.yml` — assumed to be provided by
  infrastructure the operator already has in front of the stack (ADR 006).
- **Single-node ClickHouse and PostgreSQL** — no replication/clustering (ADR 006).
- **In-process-only rate limiting** — correct for today's single-`api`-replica topology; would need
  a shared store if `api` is ever horizontally scaled (`apps/api/README.md`'s "Rate limiting").
- **No metrics/APM endpoint** — structured JSON logs on stdout are the only observability surface
  today (ADR 006).
- **No automated backup scheduling** — the backup/restore scripts exist and are drill-tested, but
  nothing in this repository invokes them on a schedule (ADR 008).
- **Flat 30-day retention, no tiering** — ClickHouse's `TTL` on both tables is a single fixed
  window; there is no hot/warm/cold tiering or downsampling.
- **No load-testing tooling** in this repository.
- **No HTTP endpoint yet to rotate/revoke an API key or create a second dashboard user** — both
  require direct database access today (`apps/api/README.md`'s "Provisioning" section documents
  the exact SQL).

## License

[Apache License 2.0](./LICENSE).
