#!/usr/bin/env bash
# Restore verification drill for Vigil backups (Phase 4D, F8). See
# docs/decisions/008-backup-restore.md.
#
# Restores a backup produced by pg_backup.sh / clickhouse_backup.sh into a
# freshly created, disposable target (a new database, or a new table) and
# reports what landed there -- proving a backup is actually restorable
# without ever touching the live production data or schema it was taken
# from. This is deliberately additive-only: it never drops, truncates, or
# overwrites anything that already exists, so there is no destructive path
# for an unsafe target name to reach.
#
# SAFETY: refuses to run unless the target name obviously marks itself as
# disposable (must contain "drill") and is not one of this deployment's real
# database/table names, AND the --confirm-disposable flag is passed
# explicitly, AND the container being used doesn't look like the production
# one ("vigil-prod*"). None of these checks can be silently skipped.
#
# Usage:
#   infrastructure/backup/restore_drill.sh postgres <backup.dump> [target-db] --confirm-disposable
#   infrastructure/backup/restore_drill.sh clickhouse <backup.native> <spans|evaluation_results> [target-table] --confirm-disposable
#
# Configurable via environment (defaults point at the LOCAL DEV stack --
# infrastructure/docker-compose.yml -- never the production one):
#   POSTGRES_DRILL_CONTAINER     (default: infrastructure-postgres-1)
#   POSTGRES_DRILL_USER          (default: vigil)
#   CLICKHOUSE_DRILL_CONTAINER   (default: infrastructure-clickhouse-1)
#   CLICKHOUSE_DRILL_USER        (default: vigil)
#   CLICKHOUSE_DRILL_PASSWORD    (default: vigil)
#   CLICKHOUSE_DRILL_DB          (default: vigil)

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage:
  restore_drill.sh postgres <backup.dump> [target-db] --confirm-disposable
  restore_drill.sh clickhouse <backup.native> <spans|evaluation_results> [target-table] --confirm-disposable
EOF
    exit 1
}

# Rejects anything that isn't obviously a one-off drill target: must contain
# "drill", and must not exactly match one of this deployment's real
# database/table names, however it's cased.
is_target_safe() {
    local target="$1"
    local lower
    lower="$(printf '%s' "$target" | tr '[:upper:]' '[:lower:]')"

    case "$lower" in
        vigil|vigil_prod|vigil-prod|production|prod|spans|evaluation_results)
            return 1
            ;;
    esac

    case "$lower" in
        *drill*) return 0 ;;
        *) return 1 ;;
    esac
}

require_confirm_disposable() {
    local found=0
    for arg in "$@"; do
        [ "$arg" = "--confirm-disposable" ] && found=1
    done
    if [ "$found" -ne 1 ]; then
        echo "ERROR: this drill performs a real restore and requires --confirm-disposable to run." >&2
        exit 1
    fi
}

reject_production_container() {
    local container="$1"
    case "$(printf '%s' "$container" | tr '[:upper:]' '[:lower:]')" in
        vigil-prod*)
            echo "ERROR: refusing to run the restore drill against '${container}' -- it looks like the production container." >&2
            echo "       This drill is for disposable dev/test targets only." >&2
            exit 1
            ;;
    esac
}

[ "$#" -ge 1 ] || usage
MODE="$1"
shift

case "$MODE" in
    postgres)
        [ "$#" -ge 1 ] || usage
        BACKUP_FILE="$1"
        shift

        TARGET_DB=""
        if [ "$#" -ge 1 ] && [ "${1#--}" = "$1" ]; then
            TARGET_DB="$1"
            shift
        fi
        TARGET_DB="${TARGET_DB:-vigil_restore_drill_$(date -u +%Y%m%dT%H%M%SZ)}"

        require_confirm_disposable "$@"

        POSTGRES_DRILL_CONTAINER="${POSTGRES_DRILL_CONTAINER:-infrastructure-postgres-1}"
        POSTGRES_DRILL_USER="${POSTGRES_DRILL_USER:-vigil}"
        reject_production_container "$POSTGRES_DRILL_CONTAINER"

        if ! is_target_safe "$TARGET_DB"; then
            echo "ERROR: target database name '${TARGET_DB}' does not look like a disposable drill target." >&2
            echo "       It must contain \"drill\" and must not be a real database name." >&2
            exit 1
        fi

        [ -f "$BACKUP_FILE" ] || { echo "ERROR: backup file not found: $BACKUP_FILE" >&2; exit 1; }
        if ! docker inspect "$POSTGRES_DRILL_CONTAINER" >/dev/null 2>&1; then
            echo "ERROR: container '${POSTGRES_DRILL_CONTAINER}' does not exist or is not reachable." >&2
            exit 1
        fi

        echo "Restore drill: postgres -> database '${TARGET_DB}' (container '${POSTGRES_DRILL_CONTAINER}')"

        CONTAINER_TMP_PATH="/tmp/restore_drill_$(basename "$BACKUP_FILE")"
        docker cp "$BACKUP_FILE" "$POSTGRES_DRILL_CONTAINER:$CONTAINER_TMP_PATH"

        docker exec "$POSTGRES_DRILL_CONTAINER" \
            psql -U "$POSTGRES_DRILL_USER" -d postgres -c "CREATE DATABASE \"$TARGET_DB\" OWNER $POSTGRES_DRILL_USER;"

        docker exec "$POSTGRES_DRILL_CONTAINER" \
            pg_restore -U "$POSTGRES_DRILL_USER" -d "$TARGET_DB" "$CONTAINER_TMP_PATH"

        docker exec "$POSTGRES_DRILL_CONTAINER" rm -f "$CONTAINER_TMP_PATH" >/dev/null 2>&1 || true

        echo ""
        echo "=== Restored tables ==="
        docker exec "$POSTGRES_DRILL_CONTAINER" \
            psql -U "$POSTGRES_DRILL_USER" -d "$TARGET_DB" -c "\dt"

        echo "=== Row counts ==="
        docker exec "$POSTGRES_DRILL_CONTAINER" psql -U "$POSTGRES_DRILL_USER" -d "$TARGET_DB" -c "
            SELECT
                schemaname,
                relname AS table_name,
                n_live_tup AS approx_row_count
            FROM pg_stat_user_tables
            ORDER BY relname;
        "

        echo ""
        echo "Restore drill succeeded. This is a disposable database -- clean it up when done:"
        echo "  docker exec ${POSTGRES_DRILL_CONTAINER} psql -U ${POSTGRES_DRILL_USER} -d postgres -c 'DROP DATABASE \"${TARGET_DB}\";'"
        ;;

    clickhouse)
        [ "$#" -ge 2 ] || usage
        BACKUP_FILE="$1"
        SOURCE_TABLE="$2"
        shift 2

        case "$SOURCE_TABLE" in
            spans|evaluation_results) ;;
            *)
                echo "ERROR: source table must be 'spans' or 'evaluation_results', got '${SOURCE_TABLE}'." >&2
                exit 1
                ;;
        esac

        TARGET_TABLE=""
        if [ "$#" -ge 1 ] && [ "${1#--}" = "$1" ]; then
            TARGET_TABLE="$1"
            shift
        fi
        TARGET_TABLE="${TARGET_TABLE:-${SOURCE_TABLE}_restore_drill_$(date -u +%Y%m%dT%H%M%SZ)}"

        require_confirm_disposable "$@"

        CLICKHOUSE_DRILL_CONTAINER="${CLICKHOUSE_DRILL_CONTAINER:-infrastructure-clickhouse-1}"
        CLICKHOUSE_DRILL_USER="${CLICKHOUSE_DRILL_USER:-vigil}"
        CLICKHOUSE_DRILL_PASSWORD="${CLICKHOUSE_DRILL_PASSWORD:-vigil}"
        CLICKHOUSE_DRILL_DB="${CLICKHOUSE_DRILL_DB:-vigil}"
        reject_production_container "$CLICKHOUSE_DRILL_CONTAINER"

        if ! is_target_safe "$TARGET_TABLE"; then
            echo "ERROR: target table name '${TARGET_TABLE}' does not look like a disposable drill target." >&2
            echo "       It must contain \"drill\" and must not be a real table name." >&2
            exit 1
        fi

        [ -f "$BACKUP_FILE" ] || { echo "ERROR: backup file not found: $BACKUP_FILE" >&2; exit 1; }
        if ! docker inspect "$CLICKHOUSE_DRILL_CONTAINER" >/dev/null 2>&1; then
            echo "ERROR: container '${CLICKHOUSE_DRILL_CONTAINER}' does not exist or is not reachable." >&2
            exit 1
        fi

        ch() {
            docker exec "$CLICKHOUSE_DRILL_CONTAINER" clickhouse-client \
                --user "$CLICKHOUSE_DRILL_USER" --password "$CLICKHOUSE_DRILL_PASSWORD" "$@"
        }
        # shellcheck disable=SC2120
        chi() {
            docker exec -i "$CLICKHOUSE_DRILL_CONTAINER" clickhouse-client \
                --user "$CLICKHOUSE_DRILL_USER" --password "$CLICKHOUSE_DRILL_PASSWORD" "$@"
        }

        echo "Restore drill: clickhouse -> table '${CLICKHOUSE_DRILL_DB}.${TARGET_TABLE}' (container '${CLICKHOUSE_DRILL_CONTAINER}')"

        ch --query "CREATE TABLE ${CLICKHOUSE_DRILL_DB}.${TARGET_TABLE} AS ${CLICKHOUSE_DRILL_DB}.${SOURCE_TABLE}"

        chi --query "INSERT INTO ${CLICKHOUSE_DRILL_DB}.${TARGET_TABLE} FORMAT Native" < "$BACKUP_FILE"

        echo ""
        echo "=== Row count ==="
        ch --query "SELECT count() FROM ${CLICKHOUSE_DRILL_DB}.${TARGET_TABLE}"

        echo "=== Sample rows ==="
        ch --query "SELECT * FROM ${CLICKHOUSE_DRILL_DB}.${TARGET_TABLE} LIMIT 3 FORMAT Vertical"

        echo ""
        echo "Restore drill succeeded. This is a disposable table -- clean it up when done:"
        echo "  docker exec ${CLICKHOUSE_DRILL_CONTAINER} clickhouse-client --user ${CLICKHOUSE_DRILL_USER} --password *** --query \"DROP TABLE ${CLICKHOUSE_DRILL_DB}.${TARGET_TABLE}\""
        ;;

    *)
        usage
        ;;
esac
