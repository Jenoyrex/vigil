#!/usr/bin/env bash
# Test harness for scheduled_backup.sh / encrypt_and_verify.sh /
# apply_retention.sh -- Phase 4D, F8. Not part of CI (ci.yml/cd.yml are
# deliberately unmodified -- a GitHub-hosted runner has no way to reach a
# real Docker host's backup surface, and this repo's existing
# restore_drill.sh is likewise operator-invoked, not CI-gated). Run this
# manually, e.g. after any change to the scripts in this directory, or as
# part of the monthly restore-drill cadence ADR 008 already recommends.
#
# REQUIRES: a reachable LOCAL DEV stack (infrastructure/docker-compose.yml
# -- infrastructure-postgres-1 / infrastructure-clickhouse-1), gpg, flock,
# and Docker CLI access. Never targets, and never even reads the identity
# of, anything named vigil-prod* -- every container name used below is a
# literal, hardcoded local-dev name, never taken from an environment
# variable a caller could point at production by accident.
#
# On a host without a native Linux userland (e.g. this was developed and
# validated on Windows/Git Bash, which has neither `flock` nor a
# guaranteed-consistent `gpg`), run this INSIDE a small Linux container
# with the Docker socket mounted in, e.g.:
#
#   docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
#     -v "$(pwd)":/repo -w /repo alpine:latest sh -c \
#     "apk add --no-cache bash docker-cli gnupg util-linux coreutils \
#      findutils grep sed >/dev/null && bash infrastructure/backup/test_scheduled_backup.sh"
#
# Usage:
#   infrastructure/backup/test_scheduled_backup.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PG_CONTAINER="infrastructure-postgres-1"
CH_CONTAINER="infrastructure-clickhouse-1"

WORKDIR="$(mktemp -d)"
cleanup() {
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

PASS_COUNT=0
FAIL_COUNT=0

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo "  PASS: $1"
}
fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "  FAIL: $1"
}
section() {
    echo ""
    echo "=== $1 ==="
}

# ---- prerequisite checks for the test harness itself -----------------------
for tool in gpg flock docker; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: this test harness requires '$tool' on PATH -- see this script's own header for how to run it inside a Linux container if your host lacks it." >&2
        exit 1
    fi
done
if ! docker inspect "$PG_CONTAINER" >/dev/null 2>&1 || ! docker inspect "$CH_CONTAINER" >/dev/null 2>&1; then
    echo "ERROR: local dev stack not reachable (expected containers '$PG_CONTAINER' and '$CH_CONTAINER' -- see infrastructure/docker-compose.yml)." >&2
    exit 1
fi

PASSPHRASE_FILE="$WORKDIR/passphrase"
echo "test-passphrase-$(date -u +%s)" > "$PASSPHRASE_FILE"
chmod 600 "$PASSPHRASE_FILE"

# Exported once so every scheduled_backup.sh invocation below inherits them
# automatically -- a per-test-case override (e.g. `POSTGRES_CONTAINER=...
# scheduled_backup.sh`) still works normally on top of these, since a
# literal `VAR=val command` prefix always takes precedence over an
# already-exported value for that one child process. (A shell FUNCTION
# cannot be used the same way here: `VAR=val VAR2=val2` with no command
# inside a function body only sets those variables in the function's own
# scope and silently ignores any trailing "command" passed as an argument
# -- an earlier draft of this harness made exactly that mistake.)
export POSTGRES_USER=vigil
export POSTGRES_DB=vigil
export POSTGRES_CONTAINER="$PG_CONTAINER"
export CLICKHOUSE_USER=vigil
export CLICKHOUSE_PASSWORD=vigil
export CLICKHOUSE_DB=vigil
export CLICKHOUSE_CONTAINER="$CH_CONTAINER"
export BACKUP_ENCRYPTION_PASSPHRASE_FILE="$PASSPHRASE_FILE"

# =============================================================================
section "encrypt_and_verify.sh: valid input encrypts and round-trips"
# =============================================================================
PLAIN="$WORKDIR/plain1.txt"
head -c 4000 /dev/urandom | base64 > "$PLAIN"
if OUT=$(BACKUP_ENCRYPTION_PASSPHRASE_FILE="$PASSPHRASE_FILE" \
    "$SCRIPT_DIR/encrypt_and_verify.sh" "$PLAIN" "$WORKDIR/enc1" 2>"$WORKDIR/err.log")
then
    if [ -s "$OUT" ] && gpg --batch --yes --pinentry-mode loopback --passphrase-file "$PASSPHRASE_FILE" \
        --decrypt "$OUT" 2>/dev/null | diff -q - "$PLAIN" >/dev/null
    then
        pass "valid file encrypts, verifies, and decrypts byte-identical"
    else
        fail "encrypted output did not round-trip to the original content"
    fi
else
    fail "encrypt_and_verify.sh unexpectedly failed on valid input: $(cat "$WORKDIR/err.log")"
fi

# =============================================================================
section "encrypt_and_verify.sh: empty/corrupt input is rejected"
# =============================================================================
touch "$WORKDIR/empty.txt"
if BACKUP_ENCRYPTION_PASSPHRASE_FILE="$PASSPHRASE_FILE" \
    "$SCRIPT_DIR/encrypt_and_verify.sh" "$WORKDIR/empty.txt" "$WORKDIR/enc2" >/dev/null 2>&1
then
    fail "empty plaintext input was NOT rejected"
else
    if [ ! -e "$WORKDIR/enc2" ] || [ -z "$(ls -A "$WORKDIR/enc2" 2>/dev/null)" ]; then
        pass "empty plaintext input rejected, no output artifact left behind"
    else
        fail "empty plaintext input rejected but left a stray output artifact"
    fi
fi

# A .gpg file truncated to just its header decrypts to empty output with
# exit 0 (verified empirically against this gpg version) -- confirms the
# decrypted-byte-count check, not just gpg's exit code, actually fires.
cp "$OUT" "$WORKDIR/truncated.gpg" 2>/dev/null || gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file "$PASSPHRASE_FILE" --cipher-algo AES256 --symmetric \
    --output "$WORKDIR/truncated.gpg" "$PLAIN"
head -c 10 "$WORKDIR/truncated.gpg" > "$WORKDIR/truncated_small.gpg"
DECRYPT_TMP="$WORKDIR/decrypt_check.tmp"
gpg --batch --yes --pinentry-mode loopback --passphrase-file "$PASSPHRASE_FILE" \
    --output "$DECRYPT_TMP" --decrypt "$WORKDIR/truncated_small.gpg" >/dev/null 2>&1
if [ ! -s "$DECRYPT_TMP" ]; then
    pass "header-only-truncated .gpg correctly produces empty decrypted output (confirms verification catches this, not just gpg's exit code)"
else
    fail "header-only-truncated .gpg unexpectedly decrypted to non-empty content"
fi
rm -f "$DECRYPT_TMP"

# =============================================================================
section "apply_retention.sh: keeps only eligible artifacts"
# =============================================================================
RETENTION_DIR="$WORKDIR/retention"
mkdir -p "$RETENTION_DIR"
mk_ts() { date -u -d "@$(( $(date -u +%s) - $1 * 86400 ))" +%Y%m%dT%H%M%SZ; }
touch "$RETENTION_DIR/vigil_postgres_vigil_$(mk_ts 5).dump.gpg"       # keep: daily
touch "$RETENTION_DIR/vigil_postgres_vigil_$(mk_ts 29).dump.gpg"      # keep: daily
touch "$RETENTION_DIR/vigil_postgres_vigil_$(mk_ts 45).dump.gpg"      # delete: same iso-week as 46d, later
touch "$RETENTION_DIR/vigil_postgres_vigil_$(mk_ts 46).dump.gpg"      # keep: weekly (earliest of its week)
touch "$RETENTION_DIR/vigil_postgres_vigil_$(mk_ts 95).dump.gpg"      # delete: beyond weekly window
touch "$RETENTION_DIR/vigil_clickhouse_spans_$(mk_ts 10).native.gpg"       # keep
touch "$RETENTION_DIR/vigil_clickhouse_spans_$(mk_ts 36).native.gpg"       # delete
"$SCRIPT_DIR/apply_retention.sh" "$RETENTION_DIR" >"$WORKDIR/retention.log" 2>&1
REMAINING="$(ls -1 "$RETENTION_DIR")"
EXPECT_KEPT_PG=3   # 5d, 29d, 46d
EXPECT_KEPT_CH=1   # 10d
ACTUAL_KEPT_PG=$(echo "$REMAINING" | grep -c "vigil_postgres_" || true)
ACTUAL_KEPT_CH=$(echo "$REMAINING" | grep -c "vigil_clickhouse_" || true)
if [ "$ACTUAL_KEPT_PG" -eq "$EXPECT_KEPT_PG" ] && [ "$ACTUAL_KEPT_CH" -eq "$EXPECT_KEPT_CH" ]; then
    pass "retention kept exactly the expected artifacts (postgres: $ACTUAL_KEPT_PG/$EXPECT_KEPT_PG, clickhouse: $ACTUAL_KEPT_CH/$EXPECT_KEPT_CH)"
else
    fail "retention kept an unexpected set (postgres: $ACTUAL_KEPT_PG, expected $EXPECT_KEPT_PG; clickhouse: $ACTUAL_KEPT_CH, expected $EXPECT_KEPT_CH)"
    cat "$WORKDIR/retention.log"
fi

# =============================================================================
section "scheduled_backup.sh: missing required configuration fails safely"
# =============================================================================
NOCONFIG_DIR="$WORKDIR/noconfig"
if env -u POSTGRES_USER POSTGRES_DB=vigil CLICKHOUSE_USER=vigil CLICKHOUSE_PASSWORD=vigil \
    CLICKHOUSE_DB=vigil BACKUP_ENCRYPTION_PASSPHRASE_FILE="$PASSPHRASE_FILE" BACKUP_OFFHOST_COMMAND=skip \
    BACKUP_DIR="$NOCONFIG_DIR" "$SCRIPT_DIR/scheduled_backup.sh" >/dev/null 2>&1
then
    fail "missing POSTGRES_USER did not cause a failure"
else
    if [ ! -d "$NOCONFIG_DIR" ]; then
        pass "missing required config fails closed before touching anything"
    else
        fail "missing required config failed, but still created BACKUP_DIR"
    fi
fi

# =============================================================================
section "scheduled_backup.sh: backup-step failure stops the pipeline"
# =============================================================================
BADCONTAINER_DIR="$WORKDIR/badcontainer"
mkdir -p "$BADCONTAINER_DIR"
if POSTGRES_CONTAINER=nonexistent-container BACKUP_OFFHOST_COMMAND=skip \
    BACKUP_DIR="$BADCONTAINER_DIR" "$SCRIPT_DIR/scheduled_backup.sh" >/dev/null 2>&1
then
    fail "an unreachable postgres container did not cause a failure"
else
    if ! ls "$BADCONTAINER_DIR"/*.gpg >/dev/null 2>&1; then
        pass "backup-step failure stops before any encrypted artifact is produced"
    else
        fail "backup-step failure still left an encrypted artifact behind"
    fi
fi

# =============================================================================
section "scheduled_backup.sh: encryption failure stops before off-host copy"
# =============================================================================
FAKEBIN="$WORKDIR/fakebin"
mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/gpg" <<'EOF'
#!/bin/sh
echo "gpg: fatal: simulated failure for testing" >&2
exit 2
EOF
chmod +x "$FAKEBIN/gpg"
ENCFAIL_DIR="$WORKDIR/encfail"
mkdir -p "$ENCFAIL_DIR"
OFFHOST_MARKER="$WORKDIR/offhost_marker"
rm -f "$OFFHOST_MARKER"
if PATH="$FAKEBIN:$PATH" BACKUP_OFFHOST_COMMAND="touch $OFFHOST_MARKER" \
    BACKUP_DIR="$ENCFAIL_DIR" "$SCRIPT_DIR/scheduled_backup.sh" >/dev/null 2>&1
then
    fail "a failing gpg did not cause scheduled_backup.sh to fail"
else
    if [ ! -f "$OFFHOST_MARKER" ]; then
        pass "encryption failure stops the pipeline before the off-host command ever runs"
    else
        fail "off-host command ran despite encryption failure"
    fi
fi

# =============================================================================
section "scheduled_backup.sh: off-host failure preserves local backups, skips retention, keeps prior known-good backup"
# =============================================================================
OFFHOSTFAIL_DIR="$WORKDIR/offhostfail"
mkdir -p "$OFFHOSTFAIL_DIR"
# Seed a fake "previous known-good" backup, 5 days old.
PRIOR_GOOD="$OFFHOSTFAIL_DIR/vigil_postgres_vigil_$(mk_ts 5).dump.gpg"
echo "prior good backup" | gpg --batch --yes --pinentry-mode loopback --passphrase-file "$PASSPHRASE_FILE" \
    --cipher-algo AES256 --symmetric --output "$PRIOR_GOOD" -
if BACKUP_OFFHOST_COMMAND=false BACKUP_DIR="$OFFHOSTFAIL_DIR" \
    "$SCRIPT_DIR/scheduled_backup.sh" >/dev/null 2>&1
then
    fail "a failing off-host command did not cause scheduled_backup.sh to fail"
else
    ok=1
    [ -f "$PRIOR_GOOD" ] || { fail "prior known-good backup was deleted despite this run's off-host failure"; ok=0; }
    ls "$OFFHOSTFAIL_DIR"/vigil_postgres_vigil_*.dump.gpg >/dev/null 2>&1 || { fail "new local encrypted backup was not preserved after off-host failure"; ok=0; }
    [ -f "$OFFHOSTFAIL_DIR/.last_success_utc" ] && { fail ".last_success_utc was written despite a failed run"; ok=0; }
    [ "$ok" -eq 1 ] && pass "off-host failure preserves both the prior known-good backup and this run's new local backups, and does not mark success"
fi

# =============================================================================
section "scheduled_backup.sh: no secrets appear in output"
# =============================================================================
FULLRUN_DIR="$WORKDIR/fullrun"
mkdir -p "$FULLRUN_DIR"
OFFHOST_DEST="$WORKDIR/offhost_dest"
mkdir -p "$OFFHOST_DEST"
BACKUP_OFFHOST_COMMAND="cp \"\$1\" $OFFHOST_DEST/" BACKUP_DIR="$FULLRUN_DIR" \
    "$SCRIPT_DIR/scheduled_backup.sh" >"$WORKDIR/fullrun.log" 2>&1
FULLRUN_EXIT=$?
if [ "$FULLRUN_EXIT" -eq 0 ] && [ -f "$FULLRUN_DIR/.last_success_utc" ]; then
    pass "a fully successful run completes and writes .last_success_utc"
else
    fail "a fully successful run did not complete as expected (exit $FULLRUN_EXIT)"
    cat "$WORKDIR/fullrun.log"
fi
if grep -qF "$(cat "$PASSPHRASE_FILE")" "$WORKDIR/fullrun.log" 2>/dev/null; then
    fail "passphrase content appeared in scheduled_backup.sh output"
else
    pass "passphrase content never appears in scheduled_backup.sh output"
fi
if grep -qE -- "--password vigil|--password=vigil|CLICKHOUSE_PASSWORD=vigil " "$WORKDIR/fullrun.log"; then
    fail "raw ClickHouse password value appeared in scheduled_backup.sh output"
else
    pass "raw ClickHouse password value never appears in scheduled_backup.sh output"
fi

# =============================================================================
section "restore_drill.sh: production-target safety remains intact (regression, unmodified script)"
# =============================================================================
SOME_PG_BACKUP="$(ls "$FULLRUN_DIR"/vigil_postgres_*.dump.gpg 2>/dev/null | head -1)"
if [ -n "$SOME_PG_BACKUP" ]; then
    gpg --batch --yes --pinentry-mode loopback --passphrase-file "$PASSPHRASE_FILE" \
        --output "$WORKDIR/decrypted_for_drill.dump" --decrypt "$SOME_PG_BACKUP" 2>/dev/null

    if POSTGRES_DRILL_CONTAINER=vigil-prod-postgres-1 \
        "$SCRIPT_DIR/restore_drill.sh" postgres "$WORKDIR/decrypted_for_drill.dump" some_drill_target --confirm-disposable \
        >/dev/null 2>&1
    then
        fail "restore_drill.sh did NOT refuse a vigil-prod*-named container"
    else
        pass "restore_drill.sh still refuses any vigil-prod*-named container"
    fi

    if POSTGRES_DRILL_CONTAINER="$PG_CONTAINER" \
        "$SCRIPT_DIR/restore_drill.sh" postgres "$WORKDIR/decrypted_for_drill.dump" vigil --confirm-disposable \
        >/dev/null 2>&1
    then
        fail "restore_drill.sh did NOT refuse an unsafe (real-looking) target name"
    else
        pass "restore_drill.sh still refuses an unsafe target name"
    fi

    # =========================================================================
    section "Full disposable-data integration: scheduled_backup.sh -> decrypt -> existing restore_drill.sh"
    # =========================================================================
    DRILL_TARGET="f8_test_harness_drill_$(date -u +%Y%m%dT%H%M%SZ)"
    if DRILL_OUTPUT=$(POSTGRES_DRILL_CONTAINER="$PG_CONTAINER" POSTGRES_DRILL_USER=vigil \
        "$SCRIPT_DIR/restore_drill.sh" postgres "$WORKDIR/decrypted_for_drill.dump" "$DRILL_TARGET" --confirm-disposable 2>&1)
    then
        pass "a backup produced by scheduled_backup.sh, once decrypted, restores successfully through the existing, unmodified restore_drill.sh"
        docker exec "$PG_CONTAINER" psql -U vigil -d postgres -c "DROP DATABASE \"$DRILL_TARGET\";" >/dev/null 2>&1
    else
        fail "restoring a scheduled_backup.sh-produced backup through restore_drill.sh failed"
        echo "$DRILL_OUTPUT"
    fi
else
    fail "no postgres backup available from the full-run test to exercise the restore integration"
fi

# =============================================================================
section "Summary"
# =============================================================================
echo "Passed: $PASS_COUNT"
echo "Failed: $FAIL_COUNT"
if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
