#!/usr/bin/env bash
# Applies Vigil's backup retention policy to an existing BACKUP_DIR --
# Phase 4D, F8. See docs/decisions/008-backup-restore.md's "Backup
# retention" section for the policy this implements, unchanged from that
# ADR's original intent:
#
#   - PostgreSQL: keep every backup within POSTGRES_RETENTION_DAILY_DAYS
#     (default 30, "daily"); for backups older than that but within
#     POSTGRES_RETENTION_WEEKLY_DAYS (default 90, "weekly"), keep exactly
#     one per ISO week (the earliest one that week); delete anything older
#     than the weekly window entirely.
#   - ClickHouse: keep everything within CLICKHOUSE_RETENTION_DAYS
#     (default 35); delete anything older. No weekly tier -- an older
#     ClickHouse backup can never contain anything the live table's own
#     30-day TTL wouldn't already have deleted, so there is nothing a
#     longer retention window would preserve.
#
# Deliberately factored out of scheduled_backup.sh as its own script (same
# one-script-per-concern convention as encrypt_and_verify.sh) so retention
# logic can be tested in isolation against synthetic, instantly-created
# fixture filenames spanning many simulated days, without needing to wait
# for -- or backdate the mtime of -- real backup files. Age is computed
# from the UTC timestamp already embedded in each filename
# (pg_backup.sh/clickhouse_backup.sh's own naming convention), never from
# file mtime, so an off-host copy tool that alters mtimes can never affect
# what this script decides to keep.
#
# Only ever DELETES files already in BACKUP_DIR that match this
# directory's own known backup-filename patterns -- never touches anything
# else that might live there (e.g. .last_success_utc, .scheduled_backup.lock).
#
# Usage:
#   apply_retention.sh <backup-dir>
#
# Optional environment (defaults shown):
#   POSTGRES_RETENTION_DAILY_DAYS=30
#   POSTGRES_RETENTION_WEEKLY_DAYS=90
#   CLICKHOUSE_RETENTION_DAYS=35
#
# Requires bash 4+ (associative arrays, for the weekly-dedup tracking) --
# safe to assume on a real Linux server (this deployment's target
# architecture; every mainstream Linux distribution has shipped bash 4+
# for well over a decade), not necessarily on macOS's ancient default
# bash 3.2.

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: apply_retention.sh <backup-dir>" >&2
    exit 1
fi

BACKUP_DIR="$1"
POSTGRES_RETENTION_DAILY_DAYS="${POSTGRES_RETENTION_DAILY_DAYS:-30}"
POSTGRES_RETENTION_WEEKLY_DAYS="${POSTGRES_RETENTION_WEEKLY_DAYS:-90}"
CLICKHOUSE_RETENTION_DAYS="${CLICKHOUSE_RETENTION_DAYS:-35}"

if [ ! -d "$BACKUP_DIR" ]; then
    echo "ERROR: backup directory not found: $BACKUP_DIR" >&2
    exit 1
fi

# Extracts the embedded YYYYMMDDTHHMMSSZ UTC timestamp from a backup
# filename and prints its epoch-seconds equivalent. Matched via regex, not
# positional field-splitting on "_": table names like "evaluation_results"
# contain an underscore themselves, which would make naive
# `cut -d_ -fN` extraction wrong -- the timestamp's own fixed shape
# (8 digits, "T", 6 digits, "Z") is unambiguous regardless of how many
# underscores precede it.
epoch_from_filename() {
    local filename="$1"
    local ts
    ts="$(printf '%s' "$filename" | grep -oE '[0-9]{8}T[0-9]{6}Z' | head -1)"
    if [ -z "$ts" ]; then
        return 1
    fi
    local iso="${ts:0:4}-${ts:4:2}-${ts:6:2}T${ts:9:2}:${ts:11:2}:${ts:13:2}Z"
    date -u -d "$iso" +%s
}

# Applies the daily+weekly (or daily-only, if weekly_days=0) policy to
# every file in BACKUP_DIR matching $pattern. Deletes ineligible files in
# place; prints one line per decision.
apply_policy() {
    local pattern="$1"
    local daily_days="$2"
    local weekly_days="$3"
    local now_epoch
    now_epoch="$(date -u +%s)"

    # epoch<TAB>path, one per matching file, sorted ascending by epoch so
    # the weekly tier's "keep the earliest file of each ISO week" rule can
    # be applied with a simple first-seen-wins pass.
    local entries=()
    local f
    shopt -s nullglob
    for f in "$BACKUP_DIR"/$pattern; do
        local epoch
        if ! epoch="$(epoch_from_filename "$(basename "$f")")"; then
            echo "  SKIP (no parseable timestamp): $(basename "$f")"
            continue
        fi
        entries+=("$epoch"$'\t'"$f")
    done
    shopt -u nullglob

    if [ "${#entries[@]}" -eq 0 ]; then
        return 0
    fi

    local sorted
    sorted="$(printf '%s\n' "${entries[@]}" | sort -n -t $'\t' -k1,1)"

    declare -A seen_weeks=()
    local epoch path age_days week_key
    while IFS=$'\t' read -r epoch path; do
        [ -z "$epoch" ] && continue
        age_days=$((  (now_epoch - epoch) / 86400  ))

        if [ "$age_days" -le "$daily_days" ]; then
            echo "  KEEP (within ${daily_days}d daily window, age ${age_days}d): $(basename "$path")"
            continue
        fi

        if [ "$weekly_days" -gt 0 ] && [ "$age_days" -le "$weekly_days" ]; then
            week_key="$(date -u -d "@$epoch" +%G-%V)"
            if [ -z "${seen_weeks[$week_key]+x}" ]; then
                seen_weeks[$week_key]=1
                echo "  KEEP (weekly tier, first backup of ISO week ${week_key}, age ${age_days}d): $(basename "$path")"
            else
                echo "  DELETE (weekly tier, week ${week_key} already has a kept backup, age ${age_days}d): $(basename "$path")"
                rm -f "$path"
            fi
            continue
        fi

        local window_days="$daily_days"
        [ "$weekly_days" -gt 0 ] && window_days="$weekly_days"
        echo "  DELETE (older than ${window_days}d retention window, age ${age_days}d): $(basename "$path")"
        rm -f "$path"
    done <<< "$sorted"
}

echo "Applying PostgreSQL retention (daily ${POSTGRES_RETENTION_DAILY_DAYS}d + weekly through ${POSTGRES_RETENTION_WEEKLY_DAYS}d)..."
apply_policy "vigil_postgres_*.dump.gpg" "$POSTGRES_RETENTION_DAILY_DAYS" "$POSTGRES_RETENTION_WEEKLY_DAYS"

echo "Applying ClickHouse retention (${CLICKHOUSE_RETENTION_DAYS}d, no weekly tier)..."
apply_policy "vigil_clickhouse_*.native.gpg" "$CLICKHOUSE_RETENTION_DAYS" 0

echo "Retention cleanup complete."
