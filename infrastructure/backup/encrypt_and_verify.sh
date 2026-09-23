#!/usr/bin/env bash
# Encrypts one plaintext backup file with GPG (AES256, symmetric,
# passphrase-file-driven) and verifies the result -- Phase 4D, F8. See
# docs/decisions/008-backup-restore.md's "Encryption" section.
#
# Deliberately factored out of scheduled_backup.sh as its own single-purpose
# script (mirroring pg_backup.sh/clickhouse_backup.sh/restore_drill.sh's own
# one-script-per-concern convention in this directory) so it can be tested
# in isolation against synthetic empty/corrupt input without needing a real
# pg_dump/ClickHouse export for every edge case -- see
# test_scheduled_backup.sh.
#
# Verification is two-layered, not just "gpg exited 0":
#   1. the plaintext input itself must be non-empty (an empty "backup" is
#      never encrypted -- there is nothing meaningful to protect, and
#      encrypting it would only hide the real underlying failure from
#      whichever earlier step produced it).
#   2. the resulting .gpg file must itself be non-empty AND actually
#      decryptable back out (a truncated/corrupted write during encryption
#      can produce a non-empty file that still cannot be decrypted -- a
#      size check alone would miss that). Decryption output is discarded
#      (verification only needs gpg's own exit code), so this costs disk
#      I/O but never doubles memory for a large backup.
#
# Usage:
#   BACKUP_ENCRYPTION_PASSPHRASE_FILE=/path/to/passphrase \
#     encrypt_and_verify.sh <plaintext-file> <output-dir>
#
# On success, prints the resulting .gpg path as the ONLY line of stdout
# (every other message goes to stderr, so a caller can safely capture just
# the path via command substitution) and exits 0. On any failure, removes
# any partial output, prints a specific error to stderr, and exits
# non-zero. The plaintext input itself is never modified or removed by
# this script -- the caller owns its lifecycle.

set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Usage: encrypt_and_verify.sh <plaintext-file> <output-dir>" >&2
    exit 1
fi

PLAINTEXT_FILE="$1"
OUTPUT_DIR="$2"

BACKUP_ENCRYPTION_PASSPHRASE_FILE="${BACKUP_ENCRYPTION_PASSPHRASE_FILE:?BACKUP_ENCRYPTION_PASSPHRASE_FILE must be set to an operator-managed passphrase file outside the repository}"

if ! command -v gpg >/dev/null 2>&1; then
    echo "ERROR: gpg is not installed on this host. Install GnuPG (e.g. 'apt-get install gnupg' on Debian/Ubuntu, 'apk add gnupg' on Alpine) before running backups, or switch the encryption mechanism -- see docs/decisions/008-backup-restore.md." >&2
    exit 1
fi

if [ ! -f "$BACKUP_ENCRYPTION_PASSPHRASE_FILE" ]; then
    echo "ERROR: passphrase file not found: $BACKUP_ENCRYPTION_PASSPHRASE_FILE" >&2
    exit 1
fi

if [ ! -f "$PLAINTEXT_FILE" ]; then
    echo "ERROR: input file not found: $PLAINTEXT_FILE" >&2
    exit 1
fi

if [ ! -s "$PLAINTEXT_FILE" ]; then
    echo "ERROR: refusing to encrypt an empty file (0 bytes): $PLAINTEXT_FILE -- this indicates the backup step that produced it failed silently rather than that there is nothing worth protecting." >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

BASENAME="$(basename "$PLAINTEXT_FILE")"
OUTPUT_FILE="$OUTPUT_DIR/${BASENAME}.gpg"

cleanup_partial() {
    rm -f "$OUTPUT_FILE"
}

ENCRYPT_ERR=""
if ! ENCRYPT_ERR=$(gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file "$BACKUP_ENCRYPTION_PASSPHRASE_FILE" \
    --cipher-algo AES256 --symmetric \
    --output "$OUTPUT_FILE" \
    "$PLAINTEXT_FILE" 2>&1 >/dev/null)
then
    echo "ERROR: gpg encryption failed for $PLAINTEXT_FILE:" >&2
    echo "$ENCRYPT_ERR" | sed 's/^/  /' >&2
    cleanup_partial
    exit 1
fi

if [ ! -s "$OUTPUT_FILE" ]; then
    echo "ERROR: encrypted output is empty: $OUTPUT_FILE" >&2
    cleanup_partial
    exit 1
fi

# Decrypted to a temp file, not /dev/null: gpg's own exit code alone is not
# a sufficient corruption check -- verified empirically (2026-09-19) that a
# .gpg file truncated down to just its packet header (before any real
# ciphertext exists) decrypts to a legitimate-looking, EMPTY output with
# exit code 0 ("gpg: AES256.CFB encrypted data", no error). Realistic
# corruption (ciphertext truncated after genuine data exists, or a flipped
# byte) correctly fails with a non-zero exit and an integrity warning, but
# that degenerate near-zero-length case would slip past an exit-code-only
# check -- so the decrypted byte count is checked too. The temp file is
# removed immediately after, regardless of outcome.
DECRYPT_TMP="$(mktemp)"
cleanup_decrypt_tmp() {
    rm -f "$DECRYPT_TMP"
}
DECRYPT_ERR=""
if ! DECRYPT_ERR=$(gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file "$BACKUP_ENCRYPTION_PASSPHRASE_FILE" \
    --output "$DECRYPT_TMP" \
    --decrypt "$OUTPUT_FILE" 2>&1)
then
    echo "ERROR: round-trip decryption verification failed for $OUTPUT_FILE -- the encrypted file may be corrupt:" >&2
    echo "$DECRYPT_ERR" | sed 's/^/  /' >&2
    cleanup_decrypt_tmp
    cleanup_partial
    exit 1
fi
if [ ! -s "$DECRYPT_TMP" ]; then
    echo "ERROR: round-trip decryption of $OUTPUT_FILE produced empty output despite a zero exit code -- the encrypted file is truncated/corrupt." >&2
    cleanup_decrypt_tmp
    cleanup_partial
    exit 1
fi
cleanup_decrypt_tmp

chmod 600 "$OUTPUT_FILE" 2>/dev/null || true

echo "$OUTPUT_FILE"
