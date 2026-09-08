# ClickHouse (local development)

Local ClickHouse instance for Vigil telemetry (`spans`), per
[`docs/decisions/003-clickhouse-telemetry-storage.md`](../../docs/decisions/003-clickhouse-telemetry-storage.md).
This is storage-layer infrastructure only — no application code connects to it yet (see "Scope"
below).

## Start

From `infrastructure`:

```bash
docker compose up -d clickhouse
```

This starts `clickhouse/clickhouse-server:24.8-alpine` with:

- **HTTP** on `localhost:8123` (used by `clickhouse-client`, the HTTP interface, and most drivers)
- **Native** on `localhost:9000` (ClickHouse's native TCP protocol)

Override the image tag, ports, or credentials via `infrastructure/.env` (copy from
`infrastructure/.env.example`) rather than editing `docker-compose.yml`. Default local
credentials are `vigil` / `vigil` against database `vigil` — fine for local development, never
used in any deployed environment.

Data persists in the `vigil_clickhouse_data` Docker volume across restarts.

## How initialization works

The official ClickHouse image runs every `.sql`/`.sh` file mounted at
`/docker-entrypoint-initdb.d/` exactly once, the first time it starts against an **empty** data
volume, in lexicographic filename order. `infrastructure/clickhouse/init/001_create_spans_table.sql`
creates the `spans` table and `infrastructure/clickhouse/init/002_create_evaluation_results_table.sql`
creates the `evaluation_results` table (per
[`docs/decisions/005-evaluation-job-storage-worker.md`](../../docs/decisions/005-evaluation-job-storage-worker.md)),
so a fresh `docker compose up -d clickhouse` is deterministic: no manual `clickhouse-client`
commands are needed to get a working schema.

The image's entrypoint only uses `CLICKHOUSE_DB` to run an initial `CREATE DATABASE`; it does
**not** pass `--database` when executing mounted `.sql` files, so an unqualified `CREATE TABLE
spans` would silently land in the `default` database instead of `vigil`. The init script works
around this by fully qualifying the table as `vigil.spans` — if you change `CLICKHOUSE_DB` away
from `vigil`, update that qualifier in the init script to match.

If you change `init/001_create_spans_table.sql` after the volume already exists, it will **not**
rerun automatically — reset the volume (see "Stop / reset" below) to pick up the change, since
this is schema-defining initialization, not a migration mechanism.

## Connect

```bash
docker compose exec clickhouse clickhouse-client --user vigil --password vigil --database vigil
```

Or over HTTP (e.g. for a quick check without a client):

```bash
curl -u vigil:vigil "http://localhost:8123/?query=SELECT+1"
```

## Verify the `spans` table

Run the verification script from anywhere in the repo:

```bash
infrastructure/clickhouse/verify.sh
```

It checks, in order: ClickHouse is reachable, `spans` exists, prints the table's `SHOW CREATE
TABLE` output to compare against ADR 003, inserts one representative test span, queries it back,
then inserts a duplicate of that same span (same `project_id`/`trace_id`/`span_id`, later
`ingested_at`) to demonstrate `ReplacingMergeTree`'s dedup behavior:

- immediately after the duplicate insert, a plain `count()` returns **2** — both rows exist
  because ClickHouse deduplicates during background merges, not at insert time (this is the
  "eventual, not immediate" dedup ADR 002/003 describe);
- the same query with `FINAL` returns **1** — the immediate-correctness read strategy any code
  path needing per-span correctness right after ingestion must use;
- forcing a merge (`OPTIMIZE TABLE spans FINAL`) and re-querying without `FINAL` also returns
  **1** — the physical duplicate has actually been collapsed, not just hidden by `FINAL`.

The script uses a fixed, obviously-fake `project_id`/`trace_id`/`span_id` so it's safe to rerun
and won't collide with real data; it leaves its test rows in place afterward (this is a local dev
sandbox, not a database you need to keep clean — reset the volume if you want a blank slate).

## Verify the `evaluation_results` table

```bash
infrastructure/clickhouse/verify_evaluation_results.sh
```

Same structure as `verify.sh` above, applied to `evaluation_results`: reachability, table
existence, `SHOW CREATE TABLE` against ADR 005 section 5, insert/query-back, then a duplicate
insert (same `evaluation_id`, later `written_at`) to demonstrate the identical
eventual/`FINAL`/`OPTIMIZE`-collapse dedup behavior. Uses its own fixed, obviously-fake
`evaluation_id` so it is safe to rerun independently of `verify.sh`.

### Why a shell script instead of an automated test suite

`apps/api` has no ClickHouse client dependency yet (deliberately — this ADR/implementation stage
is infrastructure-and-schema only, not ingestion), so a `pytest`-based integration test would
require adding one prematurely just to assert against the schema. `verify.sh` gives the same
deterministic confirmation (schema exists, matches ADR 003, insert/query/dedup all behave as
designed) using only `docker compose` and `clickhouse-client`, which are already required to run
ClickHouse locally. Once the ingestion API adds a real ClickHouse dependency to `apps/api`,
schema/dedup assertions like these should move into that project's own test suite instead.

## Stop / reset

Stop without losing data:

```bash
docker compose stop clickhouse
```

Stop and delete all ClickHouse data (including the `spans` table and any test rows), so the next
start re-runs initialization from scratch:

```bash
docker compose down -v clickhouse
```

(`-v` removes the named volume `vigil_clickhouse_data`; it does not touch the `postgres` volume.)

## Scope

This is local infrastructure and schema only:

- No Python ClickHouse client has been added to `apps/api`.
- No ingestion endpoint reads or writes this table yet.
- No production ClickHouse configuration (replication, clustering, auth beyond the basic
  user/password env vars, TLS) is defined here — that is out of scope until a deployment ADR
  addresses it.

See ADR 003 for the full schema rationale, retention/TTL behavior, payload-truncation semantics,
and deduplication model.
