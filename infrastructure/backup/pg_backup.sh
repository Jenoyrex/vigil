#!/usr/bin/env bash
# PostgreSQL backup for Vigil's control-plane database (Phase 4D, F8).
# See docs/decisions/008-backup-restore.md for the full backup/restore
# strategy this script implements one half of.
#
# Produces a `pg_dump -Fc` (custom format) logical backup by running
# `pg_dump` *inside* the already-running postgres container -- no host-level
# PostgreSQL client tools required, consistent with this deployment's
# "invoke the existing containers" approach. The dump is written to a
# container-local temp path, copied out via `docker cp`, then removed from
# the container.
#
# `pg_dump` connects over the container's local Unix socket, which this
# image's default `pg_hba.conf` trusts without a password for local
# connections -- the same reason `infrastructure/clickhouse/verify.sh` and
# this repo's own test fixtures never pass a Postgres password either. Only
# POSTGRES_USER/POSTGRES_DB are required, matching the actual connection
# requirement -- POSTGRES_PASSWORD is deliberately never read or needed here.
#
# Usage:
#   POSTGRES_USER=vigil POSTGRES_DB=vigil \
#     infrastructure/backup/pg_backup.sh
#
# Configurable via environment:
#   POSTGRES_USER       (required)  -- matches infrastructure/.env.production
#   POSTGRES_DB         (required)  -- matches infrastructure/.env.production
#   POSTGRES_CONTAINER   (default: vigil-prod-postgres-1)
#   BACKUP_DIR            (default: infrastructure/backup/output)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

POSTGRES_USER="${POSTGRES_USER:?POSTGRES_USER must be set (see infrastructure/.env.production)}"
POSTGRES_DB="${POSTGRES_DB:?POSTGRES_DB must be set (see infrastructure/.env.production)}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-vigil-prod-postgres-1}"
BACKUP_DIR="${BACKUP_DIR:-$SCRIPT_DIR/output}"

if ! docker inspect "$POSTGRES_CONTAINER" >/dev/null 2>&1; then
    echo "ERROR: container '${POSTGRES_CONTAINER}' does not exist or is not reachable." >&2
    echo "       Set POSTGRES_CONTAINER to the running postgres container's name." >&2
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
BACKUP_FILENAME="vigil_postgres_${POSTGRES_DB}_${TIMESTAMP}.dump"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_FILENAME"
CONTAINER_TMP_PATH="/tmp/${BACKUP_FILENAME}"

echo "Backing up PostgreSQL database '${POSTGRES_DB}' (container '${POSTGRES_CONTAINER}')..."

if ! docker exec "$POSTGRES_CONTAINER" \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$CONTAINER_TMP_PATH"
then
    echo "ERROR: pg_dump failed inside container '${POSTGRES_CONTAINER}'." >&2
    docker exec "$POSTGRES_CONTAINER" rm -f "$CONTAINER_TMP_PATH" >/dev/null 2>&1 || true
    exit 1
fi

if ! docker cp "$POSTGRES_CONTAINER:$CONTAINER_TMP_PATH" "$BACKUP_PATH"; then
    echo "ERROR: failed to copy the backup out of container '${POSTGRES_CONTAINER}'." >&2
    docker exec "$POSTGRES_CONTAINER" rm -f "$CONTAINER_TMP_PATH" >/dev/null 2>&1 || true
    exit 1
fi

docker exec "$POSTGRES_CONTAINER" rm -f "$CONTAINER_TMP_PATH" >/dev/null 2>&1 || true

# Best-effort: not all platforms/filesystems honor chmod (notably Windows
# host bind mounts), so a failure here is not a backup failure.
chmod 600 "$BACKUP_PATH" 2>/dev/null || true

echo "Backup written to: $BACKUP_PATH"
echo "Size: $(du -h "$BACKUP_PATH" 2>/dev/null | cut -f1)"
