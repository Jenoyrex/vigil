#!/usr/bin/env bash
# ClickHouse backup for Vigil's telemetry/evaluation-results tables
# (Phase 4D, F8). See docs/decisions/008-backup-restore.md for the full
# backup/restore strategy this script implements one half of.
#
# Backs up `spans` and `evaluation_results` using the validated
# `FORMAT Native` logical export: `clickhouse-client --query "SELECT * FROM
# <table> FORMAT Native"` run *inside* the already-running clickhouse
# container, streamed straight to a host file via `docker exec`'s stdout --
# no host-level ClickHouse client, no `docker cp` round-trip needed (`docker
# exec` without a pseudo-TTY passes the containerized process's stdout
# straight through).
#
# Deliberately does NOT use ClickHouse's native `BACKUP`/`RESTORE` SQL
# commands: this deployment's ClickHouse server config does not set
# `backups.allowed_disk`, so `BACKUP TABLE ... TO Disk(...)` fails closed
# today (verified directly against this exact image/version -- see ADR 008).
# Enabling that is a separate, not-yet-made deployment decision; `FORMAT
# Native` needs no server-side configuration change at all.
#
# Usage:
#   CLICKHOUSE_USER=vigil CLICKHOUSE_PASSWORD=*** CLICKHOUSE_DB=vigil \
#     infrastructure/backup/clickhouse_backup.sh
#
# Configurable via environment:
#   CLICKHOUSE_USER       (required)  -- matches infrastructure/.env.production
#   CLICKHOUSE_PASSWORD   (required)  -- matches infrastructure/.env.production
#   CLICKHOUSE_DB         (required)  -- matches infrastructure/.env.production
#   CLICKHOUSE_CONTAINER   (default: vigil-prod-clickhouse-1)
#   BACKUP_DIR              (default: infrastructure/backup/output)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLICKHOUSE_USER="${CLICKHOUSE_USER:?CLICKHOUSE_USER must be set (see infrastructure/.env.production)}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:?CLICKHOUSE_PASSWORD must be set (see infrastructure/.env.production)}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:?CLICKHOUSE_DB must be set (see infrastructure/.env.production)}"
CLICKHOUSE_CONTAINER="${CLICKHOUSE_CONTAINER:-vigil-prod-clickhouse-1}"
BACKUP_DIR="${BACKUP_DIR:-$SCRIPT_DIR/output}"

# The two tables this deployment has (infrastructure/clickhouse/init/*.sql).
# Not configurable -- adding a table here is a schema-level decision (see
# each init script's own "do not add columns without a follow-up ADR" note),
# not something a backup script should improvise around.
TABLES=(spans evaluation_results)

if ! docker inspect "$CLICKHOUSE_CONTAINER" >/dev/null 2>&1; then
    echo "ERROR: container '${CLICKHOUSE_CONTAINER}' does not exist or is not reachable." >&2
    echo "       Set CLICKHOUSE_CONTAINER to the running clickhouse container's name." >&2
    exit 1
fi

if ! mkdir -p "$BACKUP_DIR" 2>/dev/null; then
    echo "ERROR: could not create backup directory: $BACKUP_DIR" >&2
    exit 1
fi
if [ ! -w "$BACKUP_DIR" ]; then
    echo "ERROR: backup directory is not writable: $BACKUP_DIR" >&2
    exit 1
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

ch() {
    docker exec "$CLICKHOUSE_CONTAINER" clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
        "$@"
}

for table in "${TABLES[@]}"; do
    filename="vigil_clickhouse_${table}_${TIMESTAMP}.native"
    path="$BACKUP_DIR/$filename"

    echo "Backing up ${CLICKHOUSE_DB}.${table}..."

    if ! ch --query "SELECT * FROM ${CLICKHOUSE_DB}.${table} FORMAT Native" > "$path"; then
        echo "ERROR: export failed for table '${table}'." >&2
        rm -f "$path"
        exit 1
    fi

    chmod 600 "$path" 2>/dev/null || true
    echo "  -> $path ($(du -h "$path" 2>/dev/null | cut -f1))"
done

echo "Done."
