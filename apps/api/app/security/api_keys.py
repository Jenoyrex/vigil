"""API-key generation and verification for telemetry ingestion.

Keys look like ``vgl_<hex>.<secret>``: ``key_prefix`` (the part before the
dot) is stored in plaintext on the ``api_keys`` row so a key can be
identified in logs/UI without exposing the secret, and the full raw key is
hashed with SHA-256 into ``key_hash`` -- the only thing ever compared against
what a client presents. The raw key itself is never stored and cannot be
recovered once issued.
"""

from __future__ import annotations

import hashlib
import secrets

API_KEY_PREFIX_SCHEME = "vgl_"


def generate_api_key() -> tuple[str, str, str]:
    """Generate a new API key. Returns (raw_key, key_prefix, key_hash).

    ``raw_key`` must be shown to the caller exactly once (it cannot be
    recovered from the database afterwards); ``key_prefix`` and ``key_hash``
    are what gets persisted on the ``APIKey`` row.
    """
    key_prefix = f"{API_KEY_PREFIX_SCHEME}{secrets.token_hex(6)}"
    secret = secrets.token_urlsafe(32)
    raw_key = f"{key_prefix}.{secret}"
    return raw_key, key_prefix, hash_api_key(raw_key)


def hash_api_key(raw_key: str) -> str:
    """Hash a presented raw key for comparison against the stored `key_hash`."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def has_expected_key_shape(raw_key: str) -> bool:
    """Cheap, no-I/O check that `raw_key` could plausibly be a Vigil key.

    This is *not* the authentication check -- it only lets the caller reject
    obviously-wrong input (e.g. a token copied from an unrelated service)
    before paying for a hash computation and a database round trip.
    """
    prefix, separator, secret = raw_key.partition(".")
    return bool(separator) and prefix.startswith(API_KEY_PREFIX_SCHEME) and len(secret) > 0
