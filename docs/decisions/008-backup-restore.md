# 8. Backup & Restore

- Status: Accepted
- Date: 2026-09-18

## Context

ADR 006 (Deployment Architecture) explicitly deferred backup/restore as a known gap outside Phase
4B's scope ("...unrelated production-readiness gaps (rate limiting, CI/CD, backup/restore,
structured logging, TLS termination) that belong to later phases"). Rate limiting and dashboard
security headers were addressed in Phase 4C/4D (ADR 007). This ADR is Phase 4D's F8 item: a real,
practical recovery story for Vigil's persistent production data, sized for this deployment's
actual architecture -- a single-node Docker Compose stack, not a distributed or multi-region
system -- rather than an overengineered cloud backup platform.

## Current persistence architecture

Two stateful services, each backed by exactly one Docker named volume, no external storage:

- **PostgreSQL** (`postgres:16-alpine`, volume `vigil_postgres_data`, database `vigil`, single
  schema, no extensions): `organizations`, `projects`, `users`, `organization_memberships`,
  `api_keys`, `evaluator_configs`, `evaluation_jobs`, `evaluation_poller_checkpoint`,
  `provisioning_bootstrap`, `alembic_version`. This is **entirely irreplaceable control-plane
  state** -- `api_keys` stores only a hash, so losing this table permanently invalidates every
  issued key with no way to recover the originals, only reissue new ones.
- **ClickHouse** (`clickhouse/clickhouse-server:24.8-alpine`, volume `vigil_clickhouse_data`,
  database `vigil`): `spans` and `evaluation_results`
  (`infrastructure/clickhouse/init/001_create_spans_table.sql` /
  `002_create_evaluation_results_table.sql`, the only schema-provisioning mechanism ClickHouse has
  here -- there is no Alembic equivalent). Both carry `TTL toDate(...) + INTERVAL 30 DAY DELETE`:
  ClickHouse itself never holds more than a rolling 30-day window regardless of backup strategy,
  which bounds both how much ever needs backing up and how long a backup is worth keeping (see
  Retention below).
- No other persistence: `apps/dashboard` and `services/worker` (runtime + poller) are stateless
  containers. `infrastructure/.env.production` (secrets/config) lives only on disk/in the
  operator's shell -- never in a container volume, never in Git (already covered by this
  repository's `.env`/`.env.*` `.gitignore` pattern).

## PostgreSQL backup procedure

`infrastructure/backup/pg_backup.sh` runs `pg_dump -Fc` (custom format, single-database, not
`pg_dumpall` -- there is exactly one database, no roles/extensions worth a cluster-wide dump)
*inside* the already-running postgres container, then copies the result out via `docker cp`. No
host-level PostgreSQL client tools required.

```bash
POSTGRES_USER=vigil POSTGRES_DB=vigil infrastructure/backup/pg_backup.sh
# optional: POSTGRES_CONTAINER (default vigil-prod-postgres-1), BACKUP_DIR (default infrastructure/backup/output)
```

- **Consistency**: `pg_dump` runs inside a single `REPEATABLE READ` transaction snapshot -- a
  fully consistent point-in-time logical backup with no downtime and no need to stop the service.
- **Credentials**: only `POSTGRES_USER`/`POSTGRES_DB` are required. `pg_dump` connects over the
  container's local Unix socket, which this image's default `pg_hba.conf` trusts without a
  password for local connections -- `POSTGRES_PASSWORD` is never read or needed by this script,
  matching how this repository's own test fixtures and `infrastructure/clickhouse/verify.sh`
  already connect locally.
- **Output**: `vigil_postgres_<db>_<UTC timestamp>.dump` in `BACKUP_DIR` (default
  `infrastructure/backup/output/`, gitignored), `chmod 600` best-effort.
- **Failure modes**: missing `POSTGRES_USER`/`POSTGRES_DB` fails immediately with a clear message
  (`set -euo pipefail` plus `${VAR:?...}`); an unreachable container, a failed `pg_dump`, or a
  failed `docker cp` each abort with a non-zero exit and a specific error, cleaning up any
  container-local temp file first.

## ClickHouse backup procedure

`infrastructure/backup/clickhouse_backup.sh` backs up both `spans` and `evaluation_results` using
`clickhouse-client --query "SELECT * FROM <table> FORMAT Native"` run inside the already-running
clickhouse container, with its stdout streamed straight to a host file (`docker exec` without a
pseudo-TTY passes the containerized process's stdout straight through -- no `docker cp` round-trip
needed).

```bash
CLICKHOUSE_USER=vigil CLICKHOUSE_PASSWORD=*** CLICKHOUSE_DB=vigil infrastructure/backup/clickhouse_backup.sh
# optional: CLICKHOUSE_CONTAINER (default vigil-prod-clickhouse-1), BACKUP_DIR (default infrastructure/backup/output)
```

- **Why not native `BACKUP`/`RESTORE`**: ClickHouse 24.8 (the deployed version, confirmed via
  `SELECT version()`) supports the modern `BACKUP`/`RESTORE` SQL commands, but this deployment's
  server configuration does not set `backups.allowed_disk` -- verified directly:
  `BACKUP TABLE vigil.spans TO Disk('default', ...)` fails with
  `Code: 318 ... 'backups.allowed_disk' configuration parameter is not set`. Enabling it is a real
  ClickHouse server-config change (a `<backups><allowed_disk>...</allowed_disk></backups>` block),
  which is **deliberately not made in this phase** -- see "Future option" below.
  `FORMAT Native` needs no server-side configuration change at all and was validated end-to-end
  (see "Restore procedure").
- **Output**: one file per table, `vigil_clickhouse_<table>_<UTC timestamp>.native` in
  `BACKUP_DIR`, `chmod 600` best-effort.
- **Partitioning**: both tables are `PARTITION BY toDate(...)`, so a `FORMAT Native` export (or,
  for the future-option filesystem approach, `ALTER TABLE ... FREEZE`) is naturally
  partition-aligned -- restricting either to the last N days' partitions is straightforward if a
  full-table export ever becomes too large to be practical.
- **TTL interaction**: restoring old partitions that the 30-day TTL would otherwise have already
  deleted is safe, not harmful -- ClickHouse simply prunes them again on its next merge cycle.
- **Failure modes**: missing `CLICKHOUSE_USER`/`CLICKHOUSE_PASSWORD`/`CLICKHOUSE_DB` fails
  immediately; a failed export for either table aborts with a non-zero exit, removing any
  partially-written file first, without silently continuing to the next table.

### Future option: native ClickHouse `BACKUP`/`RESTORE`

Left as a documented future option, not implemented now: adding `backups.allowed_disk` (or a
dedicated named disk) to the clickhouse service's server configuration in
`infrastructure/docker-compose.prod.yml` would enable `BACKUP TABLE ... TO Disk(...)` /
`RESTORE TABLE ... FROM Disk(...)`, ClickHouse's own native mechanism, as an alternative or
complement to `FORMAT Native` exports. This is an additional infrastructure change with its own
tradeoffs (disk space for backup artifacts living alongside the live data disk unless a separate
disk is configured) that was out of scope for this phase per the approved implementation
direction.

## Restore procedure

**PostgreSQL** (target must already exist; not part of this repo's automation, since the target
in a real disaster-recovery restore is a freshly created database, not a script's problem to
create in place of an operator's own decision about the new database's name):

```bash
docker exec <postgres-container> pg_restore -U <user> -d <target-db> --clean --if-exists /path/to/backup.dump
```

Then run the existing `migrate` one-shot service (`alembic upgrade head`) before starting
`api`/`worker`/`poller`, to apply any migrations created after the backup was taken -- the dump
includes `alembic_version`, so this lands the schema at exactly the migration state it was backed
up at, and `alembic upgrade head` only applies what's newer.

**ClickHouse**:

```bash
docker exec <clickhouse-container> clickhouse-client --user <user> --password <password> \
    --query "INSERT INTO vigil.<table> FORMAT Native" < /path/to/backup.native
```

Requires the target table to already exist with the expected schema (a fresh volume already
creates it via the init scripts; a corrupted-but-present table needs no recreation either).

**`infrastructure/backup/restore_drill.sh`** exercises exactly this procedure end-to-end, but
*only* against a freshly created, obviously disposable target -- see "Periodic restore testing"
below. It is a verification tool, not the production restore procedure itself (production restore
targets the real `vigil` database/tables by name, which this drill script explicitly refuses to
touch).

## Recovery ordering

Derived directly from `infrastructure/docker-compose.prod.yml`'s existing `depends_on`/`condition`
chain, not assumed:

1. **Volumes/storage** -- ensure `vigil_postgres_data`/`vigil_clickhouse_data` exist (fresh, or
   restored from backup) before any container starts.
2. **`postgres`** -- must reach `service_healthy` (its `pg_isready` healthcheck) first.
3. **`clickhouse`** -- independently must reach `service_healthy`; on a *fresh* volume its
   init-scripts mount recreates `spans`/`evaluation_results` automatically -- on a *restored*
   volume those tables already exist and the init scripts are skipped (they only run against an
   empty volume).
4. **`migrate`** -- depends on `postgres` healthy only (not ClickHouse); runs `alembic upgrade
   head` to completion. This is the schema-verification step for Postgres; ClickHouse has no
   migration-gate equivalent.
5. **`api`** -- depends on `postgres` healthy + `clickhouse` healthy + `migrate` completed.
6. **`worker`**/**`poller`** -- same three dependencies as `api`; `poller` additionally depends on
   `api` healthy (it authenticates against api's internal job-creation endpoint over HTTP).
7. **`dashboard`** -- depends on `api` healthy only.

Postgres and ClickHouse have **no direct startup dependency on each other** (`migrate` only
touches Postgres). At the data level they're correlated by application logic (the poller reads
ClickHouse spans to create Postgres jobs; the worker reads Postgres jobs and writes ClickHouse
results) -- restoring the two stores from backups taken at *different* points in time is safe but
can produce redundant work (re-evaluating spans whose job row was lost, or vice versa), not silent
corruption: both sides are keyed by stable IDs, and the system already tolerates
eventual/duplicate writes (`ReplacingMergeTree`).

## Secrets/configuration recovery

None of `infrastructure/.env.production`'s contents are recoverable from a database backup.
Required separately: `POSTGRES_USER`/`PASSWORD`/`DB`, `CLICKHOUSE_USER`/`PASSWORD`/`DB`,
`VIGIL_API_DATABASE_URL`, `VIGIL_API_CLICKHOUSE_*`, `VIGIL_API_INTERNAL_SERVICE_TOKEN`,
`VIGIL_API_KEY` (the dashboard's own credential for calling the API), `VIGIL_API_RATE_LIMIT_*`,
and the `VIGIL_WORKER_*` equivalents. This file must be preserved in its own secure store (a
password manager or encrypted secrets vault), never committed to Git (already gitignored via this
repo's `.env`/`.env.*` pattern) and never bundled with a database backup.

No `bootstrap_secret`/`X-Vigil-Bootstrap-Token` value exists in `.env.production` or
`.env.production.example` today -- bootstrap provisioning (`app.api.deps.get_bootstrap_auth`
fails closed when unset) is deliberately disabled in this deployment, so it is not a recovery
concern here.

## Image-tag recovery

`VIGIL_API_IMAGE`/`VIGIL_DASHBOARD_IMAGE`/`VIGIL_WORKER_IMAGE` are deliberately *not* stored in
`.env.production` (see that file's own comments and ADR 006) -- they're supplied via shell
environment or CI (`.github/workflows/cd.yml`) at deploy time. Recovering the exact previously
deployed version requires that tag to be recorded somewhere outside the database backups
entirely -- the CD workflow's run history today, or a simple operational note (e.g. a
`deployed-version.txt`) if a faster lookup than the CI history is ever needed.

## Suggested RPO/RTO

Realistic baselines for this single-node, self-hosted, no-HA architecture -- not invented
precision:

- **PostgreSQL RPO: hourly to daily.** A full logical dump of this schema's current size is cheap
  (seconds), so hourly is easily achievable with a simple cron entry; daily is the pragmatic
  minimum given `api_keys`/`evaluation_jobs` are genuinely irreplaceable.
- **PostgreSQL RTO: minutes.** `pg_restore` at this size, plus `alembic upgrade head`, plus a
  stack restart, realistically well under 30 minutes including operator time.
- **ClickHouse RPO: daily.** Reasonable given the 30-day TTL already bounds total exposure --
  losing up to a day of telemetry is a real but bounded cost, not catastrophic.
- **ClickHouse RTO: tens of minutes to about an hour**, dominated by export/reload time at real
  data volumes -- untested at production scale by this ADR's drill (see "Known limitations").
- **Whole-stack RTO (total host loss)**: bounded mostly by *operator time* to provision a new
  host, install Docker, retrieve `.env.production` from its separate secure store, and pull
  images -- not by the backup/restore mechanics themselves, which are each minutes once the
  necessary files are in hand.

## Failure scenario matrix

| Scenario | Recoverable? | What's lost |
|---|---|---|
| PostgreSQL corruption/loss | Yes, to last `pg_backup.sh` run | Writes since the last backup |
| ClickHouse corruption/loss | Yes, to last `clickhouse_backup.sh` run | Telemetry/eval-results since the last backup (bounded by the 30-day TTL regardless) |
| Complete Docker host loss | Yes, if backups + `.env.production` + image tags were stored off-host | Everything, if backups only ever lived on the same host |
| Accidental deletion of a database volume | Yes, identical to the corresponding "corruption/loss" row above | Same as above |
| Deployment failure (bad image, failed migration) | Yes -- `docker compose ... pull` + `up --no-build` with the prior known-good image tag; `migrate` failing prevents `api`/`worker`/`poller` from ever starting against a half-migrated schema (already enforced by `service_completed_successfully`) | Nothing, if caught before `migrate` succeeds; rollback requires knowing the previous good image tag (see "Image-tag recovery") |
| Loss of `.env.production`/secrets | **Not recoverable from database backups.** DB passwords, the internal service token, and the dashboard's API key must be re-provisioned/rotated, requiring direct container/database access as a bootstrap path | Nothing from the databases themselves; real operational disruption until secrets are reissued |

## Backup retention

- **PostgreSQL**: keeping backups beyond 30 days is worthwhile -- control-plane data doesn't
  expire. A reasonable baseline: daily for 30 days, weekly for a further ~3 months.
- **ClickHouse**: retention beyond ~30-35 days is pointless -- production data itself never
  exceeds the tables' own `TTL DELETE` window, so an old ClickHouse backup can never contain
  anything the live table wouldn't already have deleted.

## Off-host storage, encryption, access control

- **Off-host is required, not optional**: `infrastructure/backup/output/` (this phase's default
  location) lives on the same Docker host as the live volumes. A host-level disk failure,
  accidental volume deletion, or host compromise destroys both the live data and any backup stored
  alongside it simultaneously. A real recovery story requires copying backup output to a second
  location (a different machine, or any existing off-host sync mechanism already available to the
  operator) -- this phase produces the backup files; moving them off-host is an operational step
  for whoever runs these scripts, deliberately not automated here (no cloud provider, no new
  dependency, per this phase's explicit scope).
- **Encryption**: backup contents include PII-adjacent control-plane data (org/user records) and,
  depending on payload retention settings, span input/output content -- encrypting backup files at
  rest (e.g. `gpg`-encrypting the dump/export before it leaves the host) is warranted and adds no
  new dependency (`gpg` is standard on most Linux hosts); not automated in this phase.
- **Access control**: backup files should be readable only by whichever account/process manages
  them -- the same bar `.env.production` already implicitly requires. `pg_backup.sh` and
  `clickhouse_backup.sh` both `chmod 600` their output as a best-effort baseline.
- **Should backups be committed to Git?** No, never -- they contain real customer/tenant data and
  would bloat the repository. `infrastructure/backup/output/`, `*.dump`, and `*.native` are
  gitignored (see `.gitignore`).

## Periodic restore testing

A backup that has never been restored is unverified. `infrastructure/backup/restore_drill.sh`
performs the same marker-data round-trip validated during this ADR's investigation: restore a
real backup into a freshly created, **obviously disposable** target, then report what landed
there (table list, row counts, sample rows) for the operator to compare against what the backup
should contain.

Safety, all enforced by the script itself, none skippable:

- The target name (database, for postgres; table, for clickhouse) **must contain the literal
  substring `drill`**, and must not exactly match a real database/table name (`vigil`,
  `production`, `vigil-prod`, `spans`, `evaluation_results`, etc.) -- reject otherwise. Omitting a
  target name entirely generates one automatically (`vigil_restore_drill_<UTC timestamp>`, etc.),
  always disposable by construction.
- The `--confirm-disposable` flag is required on every invocation; its absence is a hard error.
- The container the drill targets defaults to the **local dev stack**
  (`infrastructure-postgres-1`/`infrastructure-clickhouse-1`, from
  `infrastructure/docker-compose.yml`), never the production one, and the script refuses to run
  against any container whose name matches `vigil-prod*`, even if explicitly overridden.
- The drill is **additive only** -- it creates a new database/table and restores into it; it never
  drops, truncates, or overwrites anything that already exists, so there is no destructive
  operation for an unsafe target name to reach even if every other guard were somehow bypassed.
- Cleanup (dropping the disposable database/table afterward) is intentionally left to the
  operator, printed at the end of a successful run, rather than performed automatically by the
  script.

Recommended cadence: monthly, or after any schema-affecting migration/ClickHouse init-script
change.

## Known limitations

- **Not tested at production data volumes.** This ADR's validation drill (see "Testing" in the
  accompanying implementation) ran against a handful of rows in each table -- the actual restore
  time at real `spans`/`evaluation_results` volumes (potentially large, given a 30-day retention
  window across all tenants) is unmeasured and may materially exceed the RTO estimates above.
- **No automated scheduling.** `pg_backup.sh`/`clickhouse_backup.sh` are scripts an operator (or a
  cron entry, or a CI scheduled job) must invoke; nothing in this phase runs them automatically.
  Deliberately out of scope -- this phase is the backup/restore *mechanism*, not a scheduler or
  daemon (see "Portability" constraints below).
- **No off-host transfer automation.** As noted above, moving backup output off the Docker host is
  left to the operator's own existing tooling; this phase does not integrate with any specific
  remote storage.
- **ClickHouse `ALTER TABLE ... FREEZE`** (filesystem-level partition snapshot, an alternative to
  `FORMAT Native`) was confirmed to work with zero config changes during this ADR's investigation,
  but its restore side (`ATTACH PART`/`ATTACH PARTITION` from a frozen copy) is more
  filesystem-manipulation-heavy and was not implemented or drilled end-to-end here -- `FORMAT
  Native` was chosen as this phase's mechanism specifically because its restore path is simpler
  and was fully validated.
- **Native ClickHouse `BACKUP`/`RESTORE`** remains unimplemented by design this phase (see "Future
  option" above) -- revisit if `FORMAT Native` export/restore time becomes impractical at real
  data volumes.

## Consequences

- Operators get a documented, tested (at drill scale) backup/restore procedure for both
  datastores using only tools already present in the existing containers -- no new host-level
  software, no new dependency, no cloud provider.
- Backup output is gitignored by construction; a future contributor adding a new backup script
  under `infrastructure/backup/` should extend the same `.gitignore` patterns rather than
  committing output.
- Enabling native ClickHouse `BACKUP`/`RESTORE` later requires a real (if small) Compose/config
  change to `infrastructure/docker-compose.prod.yml` -- tracked here as a deliberate future
  option, not a gap discovered later.
- The restore drill's safety checks (target-name marker, `--confirm-disposable`, non-production
  container rejection, additive-only operation) should be preserved as-is by any future change to
  `restore_drill.sh` -- they are the only thing standing between this tooling and an accidental
  write to production data.
