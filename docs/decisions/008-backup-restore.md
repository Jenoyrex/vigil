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

**0. Decrypt (if the backup was produced by `scheduled_backup.sh`, i.e. every backup taken after
this revision).** Restore always operates on the decrypted plaintext; nothing in this repository
restores directly from a `.gpg` file. Decrypt to a working copy, never in place of the original:

```bash
gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file /etc/vigil/backup-passphrase \
    --output ./restore-work/backup.dump \
    --decrypt vigil_postgres_vigil_<timestamp>.dump.gpg
```

**Preserve the original backup during an incident**: always decrypt into a separate working copy
(as above) and restore from *that* -- never delete, move, or overwrite the original `.gpg` file (or
its off-host copy) until the restore has been fully verified successful. If the restore itself
reveals a problem (wrong point in time, unexpectedly missing data), the original encrypted backup
is the only way to try again or fall back to a different one.

**1. Stop the application before restoring, not just the database.** `api`/`worker`/`poller` all
hold live connections and can write during a restore, which a mid-restore `pg_restore`/ClickHouse
insert can then race against. Stop them first (`docker compose -f
infrastructure/docker-compose.prod.yml stop api worker poller dashboard`), leaving only
`postgres`/`clickhouse` running, restore into that quiescent state, then restart in the order
"Recovery ordering" below already documents (`migrate` before `api`/`worker`/`poller`).

**2. PostgreSQL** (target must already exist; not part of this repo's automation, since the target
in a real disaster-recovery restore is a freshly created database, not a script's problem to
create in place of an operator's own decision about the new database's name):

```bash
docker exec <postgres-container> pg_restore -U <user> -d <target-db> --clean --if-exists /path/to/backup.dump
```

Then run the existing `migrate` one-shot service (`alembic upgrade head`) before starting
`api`/`worker`/`poller`, to apply any migrations created after the backup was taken -- the dump
includes `alembic_version`, so this lands the schema at exactly the migration state it was backed
up at, and `alembic upgrade head` only applies what's newer.

**3. ClickHouse**:

```bash
docker exec <clickhouse-container> clickhouse-client --user <user> --password <password> \
    --query "INSERT INTO vigil.<table> FORMAT Native" < /path/to/backup.native
```

Requires the target table to already exist with the expected schema (a fresh volume already
creates it via the init scripts; a corrupted-but-present table needs no recreation either).

**4. Verify before resuming traffic.** At minimum: table list and row counts on both stores (the
same checks `restore_drill.sh` itself already prints -- run the equivalent queries by hand against
the real restored target), and a manual `GET /health`/`GET /ready` against `api` once it's back up
before pointing real traffic at it again.

**5. Mismatched application/database versions.** If the image tag being deployed is *newer* than
the one running when the backup was taken, `alembic upgrade head` (step 2) brings the schema
forward safely -- this is the normal case. If it is *older* (a rollback scenario, restoring an old
backup against a newer application version, or vice versa), there is no automated downgrade path:
Alembic's `downgrade` would need to be invoked manually and only works if the intervening
migrations were written to support it, which is not guaranteed by anything in this repository (see
"Rollback assumes schema forward/backward compatibility" in ADR 006 decision 13 -- the identical
caveat applies here to a database restore, not just an image rollback). Restoring a backup whose
schema version doesn't match the application version being run is not safe to do silently; resolve
the version mismatch deliberately before starting `api`/`worker`/`poller` against the restored
data.

**`infrastructure/backup/restore_drill.sh`** exercises exactly steps 2/3 end-to-end, but *only*
against a freshly created, obviously disposable target -- see "Periodic restore testing" below. It
is a verification tool, not the production restore procedure itself (production restore targets
the real `vigil` database/tables by name, which this drill script explicitly refuses to touch), and
it is deliberately unmodified by this revision -- its safety guards (target-name marker,
`--confirm-disposable`, non-production-container rejection, additive-only operation) remain exactly
as they were.

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
  expire. Baseline, unchanged from this ADR's original intent and now actually enforced by
  `infrastructure/backup/apply_retention.sh` (`POSTGRES_RETENTION_DAILY_DAYS=30`,
  `POSTGRES_RETENTION_WEEKLY_DAYS=90`): daily for 30 days, then exactly one backup per ISO
  week (the earliest that week) retained through 90 days (~3 months), then deleted.
- **ClickHouse**: retention beyond ~30-35 days is pointless -- production data itself never
  exceeds the tables' own `TTL DELETE` window, so an old ClickHouse backup can never contain
  anything the live table wouldn't already have deleted. Enforced by the same script
  (`CLICKHOUSE_RETENTION_DAYS=35`), no weekly tier.
- Retention age is computed from the UTC timestamp already embedded in each backup's filename
  (`pg_backup.sh`/`clickhouse_backup.sh`'s own naming convention), never from file mtime -- an
  off-host copy tool that alters mtimes on the remote side can never affect what the *local*
  cleanup decides to keep, and retention only ever runs after a fully successful backup +
  encryption + off-host cycle (see "Scheduled backup automation" below), never against a run that
  failed partway through.

## Off-host storage, encryption, access control

- **Off-host is required, not optional**: `infrastructure/backup/output/` (the default local
  location) lives on the same Docker host as the live volumes. A host-level disk failure,
  accidental volume deletion, or host compromise destroys both the live data and any backup stored
  alongside it simultaneously. A real recovery story requires copying backup output to a second
  location -- **automated as of this revision** by `infrastructure/backup/scheduled_backup.sh` via
  `BACKUP_OFFHOST_COMMAND`, an operator-supplied shell command invoked once per newly-encrypted
  file with that file's path as `$1` (e.g. `rsync -av "$1" operator@backup-host:/backups/vigil/`,
  `aws s3 cp "$1" s3://my-bucket/vigil-backups/`, `rclone copy "$1" myremote:vigil-backups/`).
  Deliberately a hook, not a bundled implementation: no cloud provider, transport, or new
  dependency is assumed or required by this repository itself -- the operator supplies whatever
  transport they already have. `BACKUP_OFFHOST_COMMAND` is required (the job refuses to run without
  it) unless explicitly set to the literal string `skip`, so a misconfigured deployment fails loud
  rather than silently running local-only.
- **Encryption**: backup contents include PII-adjacent control-plane data (org/user records) and,
  depending on payload retention settings, span input/output content -- **automated as of this
  revision** via `infrastructure/backup/encrypt_and_verify.sh`, which GPG-encrypts each backup file
  (`--symmetric --cipher-algo AES256`, passphrase supplied via `--passphrase-file`, never as a bare
  argument or env-var value) before it is moved into the persistent backup directory or copied
  off-host. This is a standard, integrity-protected OpenPGP construction, not a from-scratch
  cryptographic design -- precisely: GPG's symmetric mode is tamper-evident (its SEIP/MDC
  construction), which is a genuine, decades-proven authenticated mechanism, though not literally
  the same textbook AES-GCM/ChaCha20-Poly1305 AEAD construction a ground-up modern design (e.g.
  `age`) would use; `age` is a documented alternative for an operator who'd rather install one
  additional small static binary for that construction specifically. **Prerequisite, checked and
  failed closed, not silently assumed**: `gpg` must be installed on the host running
  `scheduled_backup.sh` -- the script verifies this at startup and refuses to run with a clear
  message if it's missing, rather than failing confusingly partway through. The passphrase itself
  lives in `BACKUP_ENCRYPTION_PASSPHRASE_FILE`, an operator-managed file outside this repository
  (same treatment as `.env.production` itself: never in Git, never in a container volume) --
  **losing this passphrase makes every existing encrypted backup permanently unrecoverable**, so it
  must be backed up too, separately from the database backups it protects.
- **Access control**: backup files should be readable only by whichever account/process manages
  them -- the same bar `.env.production` already implicitly requires. `pg_backup.sh` and
  `clickhouse_backup.sh` both `chmod 600` their raw output as a best-effort baseline;
  `encrypt_and_verify.sh` does the same for its encrypted `.gpg` output.
- **Should backups be committed to Git?** No, never -- they contain real customer/tenant data and
  would bloat the repository, encrypted or not. `infrastructure/backup/output/`, `*.dump`,
  `*.native`, and `*.gpg` are all gitignored (see `.gitignore`).

## Scheduled backup automation

`infrastructure/backup/scheduled_backup.sh` ties `pg_backup.sh`/`clickhouse_backup.sh` (both
unmodified), encryption, off-host copy, and retention cleanup into one operator-scheduled job:

```
prerequisite checks (gpg present, passphrase file readable, flock present)
    -> flock-protected (no overlapping runs)
    -> mktemp -d staging directory (plaintext dumps/exports land here, never in BACKUP_DIR)
    -> pg_backup.sh + clickhouse_backup.sh, into staging
    -> encrypt_and_verify.sh, once per file (encrypt, verify non-empty, verify round-trip decrypt)
    -> move verified .gpg files into the persistent BACKUP_DIR
    -> staging directory removed (plaintext never persists past this point)
    -> BACKUP_OFFHOST_COMMAND, once per new .gpg file
    -> apply_retention.sh (ONLY reached if every step above succeeded)
    -> BACKUP_DIR/.last_success_utc written (ONLY on full success)
```

Each stage must fully succeed (`set -euo pipefail`) before the next begins. This ordering is what
gives the "never delete a known-good backup before its replacement is verified" guarantee for
free: retention -- the only stage that ever deletes an existing file -- is last, and is simply
unreachable if the new backup, its encryption, or its off-host copy failed. If the off-host copy
fails, the local encrypted copy is left in place (not deleted), retention does not run that cycle,
and the exit is non-zero -- fix the destination and re-run.

**Usage:**

```bash
set -a; source infrastructure/.env.production; set +a   # POSTGRES_*/CLICKHOUSE_* credentials
BACKUP_ENCRYPTION_PASSPHRASE_FILE=/etc/vigil/backup-passphrase \
BACKUP_OFFHOST_COMMAND='rsync -av "$1" operator@backup-host:/backups/vigil/' \
  infrastructure/backup/scheduled_backup.sh
```

**Scheduling: host-level cron (primary), systemd timer (documented alternative).** Neither GitHub
Actions option was viable to begin with: a hosted Actions runner has no network path to this
host's `docker exec` surface at all, and this deployment deliberately runs no self-hosted runner
(a materially larger, security-sensitive architectural change nobody asked for). Between the two
real host-level options, cron is the primary, documented mechanism because it is available on
effectively every Linux distribution regardless of init system (systemd timers require systemd
specifically -- not universal, e.g. Alpine-based hosts commonly run OpenRC instead), which matters
for a deployment that is deliberately provider/host-neutral. A Docker scheduled container was
considered and rejected: it would mean running a permanent, always-on container purely to wait for
a cron-like trigger, when the host already has a scheduler built in, and the backup job already
needs host-level `docker exec` access regardless.

Example crontab entry (daily at 03:00, adjust to taste):

```cron
0 3 * * * . /etc/vigil/backup.env && /opt/vigil/infrastructure/backup/scheduled_backup.sh >> /var/log/vigil-backup.log 2>&1
```

(`/etc/vigil/backup.env` here is a small shell-sourceable file containing `source
infrastructure/.env.production` plus the `BACKUP_ENCRYPTION_PASSPHRASE_FILE`/
`BACKUP_OFFHOST_COMMAND` exports -- cron's own environment is minimal by design and does not run a
login shell, so environment setup must be explicit.)

Equivalent systemd timer, for an operator who prefers it:

```ini
# /etc/systemd/system/vigil-backup.service
[Unit]
Description=Vigil scheduled backup

[Service]
Type=oneshot
EnvironmentFile=/etc/vigil/backup.env
WorkingDirectory=/opt/vigil
ExecStart=/opt/vigil/infrastructure/backup/scheduled_backup.sh
```

```ini
# /etc/systemd/system/vigil-backup.timer
[Unit]
Description=Run Vigil scheduled backup daily

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

**Overlap protection**: `flock` (part of `util-linux`, essentially universal on real Linux
servers -- same disclosure tier as the `gpg` prerequisite above) guards against a run started
while a previous one is still in progress (e.g. it hung past the next scheduled tick); a second
concurrent invocation exits immediately with a clear message rather than racing the first.

**Failure visibility**: every stage's log lines are prefixed with that stage's name
(`[prereqs]`/`[postgres-backup]`/`[clickhouse-backup]`/`[encrypt]`/`[offhost]`/`[retention]`/
`[complete]`), so cron's own captured output (redirected to a log file, or cron's `MAILTO`) tells
an operator exactly which stage failed without needing to re-run anything. On full success only,
`BACKUP_DIR/.last_success_utc` is written -- a plain UTC timestamp any external check can poll for
staleness without this repository integrating with any specific notification service, e.g.:

```bash
# Alert if the last successful backup is more than 26 hours old (bounds one
# missed daily run plus a reasonable buffer, not just exactly 24h).
find infrastructure/backup/output/.last_success_utc -mmin +1560 2>/dev/null && echo "STALE BACKUP"
```

Wire that check into whatever monitoring the operator already has (a cron+mail dead-man's-switch,
Nagios, a simple heartbeat service, etc.) -- deliberately not implemented here, since a fake
Slack/email integration with no real credentials behind it would be worse than an honest, generic
integration point.

## Backup failure runbook

What to do when a scheduled run fails, or `.last_success_utc` goes stale:

1. **Find the failure.** Check cron's captured output (wherever the crontab entry redirects it,
   e.g. `/var/log/vigil-backup.log`) or `journalctl -u vigil-backup.service` for a systemd-timer
   setup. The `[stage]`-prefixed log lines (see above) name exactly which stage failed.
2. **`[prereqs]` failures** -- missing `gpg`/`flock`, an unreadable passphrase file, or a missing
   required environment variable. Fix the host/config issue named in the error; nothing was
   touched, so simply re-running once fixed is safe.
3. **`[postgres-backup]`/`[clickhouse-backup]` failures** -- the underlying `pg_backup.sh`/
   `clickhouse_backup.sh` failed (container unreachable, `pg_dump`/`clickhouse-client` error).
   Check the container is actually running (`docker ps`) and healthy; the existing backup output
   directory is untouched by a failure at this stage.
4. **`[encrypt]` failures** -- `gpg` itself failed, or round-trip verification caught a corrupt
   encrypted file. Re-running is safe (a fresh backup + fresh encryption attempt); if this recurs,
   check host disk space (`df -h`) and confirm the passphrase file's contents haven't changed
   unexpectedly.
5. **`[offhost]` failures** -- the operator-configured `BACKUP_OFFHOST_COMMAND` failed. The new
   local encrypted backup is safe (preserved, not deleted) -- diagnose the destination (network
   reachability, credentials, remote disk space) and either re-run this script once fixed, or
   manually copy the already-encrypted file(s) sitting in `BACKUP_DIR` off-host as a one-off.
6. **`[retention]` failures** -- this stage only ever *deletes* files past their retention window;
   a failure here does not put any current backup at risk. Investigate and re-run
   `infrastructure/backup/apply_retention.sh BACKUP_DIR` directly once fixed.
7. **Escalation**: if backups have been failing for longer than the shortest RPO this ADR commits
   to (see "Suggested RPO/RTO" below), treat it as an active incident, not routine maintenance --
   the exposure is "how much data would be unrecoverable right now," which grows every additional
   day a fix is delayed.

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

- **Still not tested at production data volumes -- an explicit operator runbook requirement, not
  something this revision claims to have solved.** Both this ADR's original validation drill and
  the new `test_scheduled_backup.sh` harness run against a handful of rows in each table, on a
  local dev stack -- the actual backup/encrypt/restore time at real `spans`/`evaluation_results`
  volumes (potentially large, given a 30-day retention window across all tenants) is unmeasured and
  may materially exceed the RTO estimates below. A true production-volume drill needs either a
  large synthetic dataset or an actual rehearsal against production-scale data, neither of which is
  safe to fabricate or automate here. **Recommended operator action**: run a full
  backup-encrypt-decrypt-restore cycle against a production-scale copy (or the real production
  data, restored into a disposable target via `restore_drill.sh`, never in place) before the first
  real incident, and re-run it after any major schema/version change -- do not treat this ADR's
  drill-scale validation as a substitute.
- **~~No automated scheduling~~ -- resolved.** `infrastructure/backup/scheduled_backup.sh`,
  invoked by a host-level cron entry (or a documented systemd-timer alternative), ties backup,
  encryption, off-host copy, and retention into one operator-scheduled job -- see "Scheduled backup
  automation" above.
- **~~No off-host transfer automation~~ -- resolved, as a hook, not a bundled transport.**
  `BACKUP_OFFHOST_COMMAND` gives `scheduled_backup.sh` an explicit, provider-neutral integration
  point (see "Off-host storage, encryption, access control" above) -- the operator still supplies
  the actual transport (rsync/rclone/a cloud CLI/etc.); this repository does not bundle one, since
  doing so would mean choosing a provider on the operator's behalf.
- **`gpg` and `flock` must be present on whatever host runs `scheduled_backup.sh`.** Checked and
  failed closed at startup (a clear error, not a confusing mid-run failure) rather than silently
  assumed -- but not guaranteed pre-installed on every possible host, the same disclosure standard
  this ADR already held ClickHouse's `FORMAT Native` mechanism to.
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
  datastores using only tools already present on the host/in the existing containers (`gpg`,
  `flock`, Docker) -- one new host-level dependency tier (`gpg`/`flock`, both checked and failed
  closed rather than assumed), no new application dependency, no cloud provider chosen on the
  operator's behalf.
- Scheduled backups are now encrypted at rest and copied off-host automatically, closing two of
  this ADR's three original gaps (scheduling, off-host transfer); the third (production-volume
  drilling) remains an explicit, documented operator runbook item, not something automated here --
  see "Known limitations" above.
- Backup output (plaintext and encrypted) is gitignored by construction; a future contributor
  adding a new backup script under `infrastructure/backup/` should extend the same `.gitignore`
  patterns rather than committing output.
- Enabling native ClickHouse `BACKUP`/`RESTORE` later requires a real (if small) Compose/config
  change to `infrastructure/docker-compose.prod.yml` -- tracked here as a deliberate future
  option, not a gap discovered later.
- The restore drill's safety checks (target-name marker, `--confirm-disposable`, non-production
  container rejection, additive-only operation) should be preserved as-is by any future change to
  `restore_drill.sh` -- they are the only thing standing between this tooling and an accidental
  write to production data. This revision leaves `restore_drill.sh` completely unmodified.
- `scheduled_backup.sh`'s own safety property (never delete an existing backup before its
  replacement is fully created, encrypted, verified, and copied off-host) is enforced entirely by
  stage ordering (retention is the last stage, unreachable on any earlier failure), not by a
  separate bookkeeping mechanism -- any future change to that script must preserve this ordering,
  not just the individual stages' own correctness.
