#!/usr/bin/env bash
# Scheduled backup orchestrator for Vigil (Phase 4D, F8). Intended to be
# invoked by a host-level cron entry (or, alternatively, a systemd timer --
# see docs/decisions/008-backup-restore.md's "Scheduling" section) on the
# production Docker host, never by GitHub Actions: a hosted Actions runner
# has no network path to this host's `docker exec` surface at all, and
# this deployment deliberately runs no self-hosted runner (see that ADR
# section for the full reasoning).
#
# Ties together, in strict order -- each stage must fully succeed before
# the next begins, via `set -euo pipefail` -- the existing, UNMODIFIED
# pg_backup.sh/clickhouse_backup.sh, plus three new steps: encryption
# (encrypt_and_verify.sh), an operator-configured off-host copy, and
# retention cleanup (apply_retention.sh). This ordering is exactly what
# gives the "never delete a known-good backup before its replacement is
# verified" guarantee: retention (the only step that ever deletes an
# existing file) is the LAST stage, unreachable unless everything before
# it -- both backups, both encryptions, both round-trip verifications, and
# the off-host copy -- already succeeded.
#
# Plaintext dumps/exports are never written directly into BACKUP_DIR: they
# land in a per-run `mktemp -d` staging directory, which is always removed
# (via an EXIT trap, success or failure) as soon as this run's files have
# been encrypted -- BACKUP_DIR only ever contains encrypted (.gpg) output.
#
# Usage (from anywhere -- paths are resolved relative to this script's own
# location):
#   set -a; source infrastructure/.env.production; set +a   # POSTGRES_*/CLICKHOUSE_* credentials
#   BACKUP_ENCRYPTION_PASSPHRASE_FILE=/etc/vigil/backup-passphrase \
#   BACKUP_OFFHOST_COMMAND='rsync -av "$1" operator@backup-host:/backups/vigil/' \
#     infrastructure/backup/scheduled_backup.sh
#
# Required environment:
#   POSTGRES_USER, POSTGRES_DB                                  (pg_backup.sh)
#   CLICKHOUSE_USER, CLICKHOUSE_PASSWORD, CLICKHOUSE_DB          (clickhouse_backup.sh)
#   BACKUP_ENCRYPTION_PASSPHRASE_FILE  -- path to an operator-managed
#     passphrase file OUTSIDE this repository, chmod 600, never committed.
#   BACKUP_OFFHOST_COMMAND  -- a shell command string this script invokes
#     once per newly-produced .gpg file, with that file's path as $1 (e.g.
#     'rsync -av "$1" host:/backups/'). Set to the literal string "skip"
#     to deliberately run local-only (a documented opt-out, e.g. for a
#     disposable test environment) -- leaving it entirely unset is a hard
#     configuration error, not a silent no-op: off-host copy is required,
#     not optional, for a real production deployment (see the ADR).
#
# Optional environment (defaults shown):
#   POSTGRES_CONTAINER=vigil-prod-postgres-1
#   CLICKHOUSE_CONTAINER=vigil-prod-clickhouse-1
#   BACKUP_DIR=infrastructure/backup/output
#   POSTGRES_RETENTION_DAILY_DAYS=30
#   POSTGRES_RETENTION_WEEKLY_DAYS=90
#   CLICKHOUSE_RETENTION_DAYS=35
#
# Failure visibility: every stage's log lines are prefixed with that
# stage's name ([prereqs]/[postgres-backup]/[clickhouse-backup]/[encrypt]/
# [offhost]/[retention]/[complete]) so an operator can tell exactly which
# stage failed
# from cron's own captured output (e.g. redirecting this script's own
# invocation to `>> /var/log/vigil-backup.log 2>&1`, or cron's MAILTO). On
# full success only, this script writes BACKUP_DIR/.last_success_utc -- a
# plain UTC timestamp an external check (any monitoring system, or a
# simple `find ... -mmin +N`) can poll for staleness without this
# repository needing to integrate with any specific notification service.
# See the ADR's "Backup failure runbook" section.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[$1] $2"; }
fail() { echo "[$1] ERROR: $2" >&2; exit 1; }

# ---- prerequisite / required-configuration checks -------------------------
POSTGRES_USER="${POSTGRES_USER:?POSTGRES_USER must be set (see infrastructure/.env.production)}"
POSTGRES_DB="${POSTGRES_DB:?POSTGRES_DB must be set (see infrastructure/.env.production)}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:?CLICKHOUSE_USER must be set (see infrastructure/.env.production)}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:?CLICKHOUSE_PASSWORD must be set (see infrastructure/.env.production)}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:?CLICKHOUSE_DB must be set (see infrastructure/.env.production)}"
BACKUP_ENCRYPTION_PASSPHRASE_FILE="${BACKUP_ENCRYPTION_PASSPHRASE_FILE:?BACKUP_ENCRYPTION_PASSPHRASE_FILE must be set to an operator-managed passphrase file outside the repository}"
BACKUP_OFFHOST_COMMAND="${BACKUP_OFFHOST_COMMAND:?BACKUP_OFFHOST_COMMAND must be set (an operator-supplied shell command, e.g. rsync/rclone/a cloud CLI) -- or the literal string 'skip' to deliberately run local-only}"

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-vigil-prod-postgres-1}"
CLICKHOUSE_CONTAINER="${CLICKHOUSE_CONTAINER:-vigil-prod-clickhouse-1}"
BACKUP_DIR="${BACKUP_DIR:-$SCRIPT_DIR/output}"
POSTGRES_RETENTION_DAILY_DAYS="${POSTGRES_RETENTION_DAILY_DAYS:-30}"
POSTGRES_RETENTION_WEEKLY_DAYS="${POSTGRES_RETENTION_WEEKLY_DAYS:-90}"
CLICKHOUSE_RETENTION_DAYS="${CLICKHOUSE_RETENTION_DAYS:-35}"

if ! command -v gpg >/dev/null 2>&1; then
    fail prereqs "gpg is not installed on this host. Install GnuPG before running scheduled backups -- see docs/decisions/008-backup-restore.md's Encryption section."
fi
if [ ! -f "$BACKUP_ENCRYPTION_PASSPHRASE_FILE" ]; then
    fail prereqs "passphrase file not found: $BACKUP_ENCRYPTION_PASSPHRASE_FILE"
fi
if ! command -v flock >/dev/null 2>&1; then
    fail prereqs "flock is not installed on this host (part of util-linux on virtually every Linux distribution). Required to prevent overlapping scheduled runs."
fi

mkdir -p "$BACKUP_DIR"

# ---- overlap protection -------------------------------------------------
LOCK_FILE="$BACKUP_DIR/.scheduled_backup.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    fail prereqs "another scheduled_backup.sh run is already in progress (lock held on $LOCK_FILE). Exiting without touching anything."
fi

# ---- staging: plaintext dumps never touch BACKUP_DIR directly -----------
STAGING_DIR="$(mktemp -d)"
cleanup_staging() {
    rm -rf "$STAGING_DIR"
}
trap cleanup_staging EXIT

log postgres-backup "starting..."
if ! BACKUP_DIR="$STAGING_DIR" POSTGRES_USER="$POSTGRES_USER" POSTGRES_DB="$POSTGRES_DB" \
    POSTGRES_CONTAINER="$POSTGRES_CONTAINER" \
    "$SCRIPT_DIR/pg_backup.sh"
then
    fail postgres-backup "pg_backup.sh failed -- see output above. No existing backup was touched."
fi
log postgres-backup "done."

log clickhouse-backup "starting..."
if ! BACKUP_DIR="$STAGING_DIR" CLICKHOUSE_USER="$CLICKHOUSE_USER" CLICKHOUSE_PASSWORD="$CLICKHOUSE_PASSWORD" \
    CLICKHOUSE_DB="$CLICKHOUSE_DB" CLICKHOUSE_CONTAINER="$CLICKHOUSE_CONTAINER" \
    "$SCRIPT_DIR/clickhouse_backup.sh"
then
    fail clickhouse-backup "clickhouse_backup.sh failed -- see output above. No existing backup was touched."
fi
log clickhouse-backup "done."

# ---- encrypt + verify every file this run produced -----------------------
NEW_ENCRYPTED_FILES=()
shopt -s nullglob
for plaintext in "$STAGING_DIR"/*; do
    [ -f "$plaintext" ] || continue
    log encrypt "encrypting $(basename "$plaintext")..."
    encrypted_path="$(BACKUP_ENCRYPTION_PASSPHRASE_FILE="$BACKUP_ENCRYPTION_PASSPHRASE_FILE" \
        "$SCRIPT_DIR/encrypt_and_verify.sh" "$plaintext" "$STAGING_DIR/encrypted")" \
        || fail encrypt "encryption/verification failed for $(basename "$plaintext"). No existing backup was touched, and nothing was copied off-host."
    NEW_ENCRYPTED_FILES+=("$encrypted_path")
    log encrypt "$(basename "$encrypted_path") verified (encrypted + round-trip decrypted OK)."
done
shopt -u nullglob

if [ "${#NEW_ENCRYPTED_FILES[@]}" -eq 0 ]; then
    fail encrypt "no backup files were produced by pg_backup.sh/clickhouse_backup.sh -- nothing to encrypt. Treating this as a failure rather than a silent no-op."
fi

# Move verified encrypted files into the persistent BACKUP_DIR only now --
# this is the first point this run touches BACKUP_DIR's real contents, and
# only ADDS new files to it (nothing existing is ever removed here).
FINAL_FILES=()
for f in "${NEW_ENCRYPTED_FILES[@]}"; do
    dest="$BACKUP_DIR/$(basename "$f")"
    mv "$f" "$dest"
    FINAL_FILES+=("$dest")
done
log encrypt "moved ${#FINAL_FILES[@]} encrypted file(s) into $BACKUP_DIR."

# ---- off-host copy --------------------------------------------------------
if [ "$BACKUP_OFFHOST_COMMAND" = "skip" ]; then
    log offhost "BACKUP_OFFHOST_COMMAND=skip -- deliberately running local-only, per operator configuration."
else
    for f in "${FINAL_FILES[@]}"; do
        log offhost "copying $(basename "$f") off-host..."
        if ! bash -c "$BACKUP_OFFHOST_COMMAND" _ "$f"; then
            fail offhost "off-host copy command failed for $(basename "$f"). The local encrypted copy at $f is preserved. Retention will NOT run this cycle -- fix the off-host destination and re-run."
        fi
    done
    log offhost "all ${#FINAL_FILES[@]} file(s) copied off-host successfully."
fi

# ---- retention cleanup: only reached after a fully successful run --------
log retention "applying retention policy..."
if ! POSTGRES_RETENTION_DAILY_DAYS="$POSTGRES_RETENTION_DAILY_DAYS" \
    POSTGRES_RETENTION_WEEKLY_DAYS="$POSTGRES_RETENTION_WEEKLY_DAYS" \
    CLICKHOUSE_RETENTION_DAYS="$CLICKHOUSE_RETENTION_DAYS" \
    "$SCRIPT_DIR/apply_retention.sh" "$BACKUP_DIR"
then
    fail retention "retention cleanup failed -- see output above. All backups just created and copied off-host this run are safe; only cleanup of OLD files may be incomplete."
fi
log retention "done."

date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/.last_success_utc"
log complete "scheduled backup completed successfully at $(cat "$BACKUP_DIR/.last_success_utc")."
