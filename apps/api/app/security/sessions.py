"""Dashboard session token generation/hashing (Phase 4D, F1).

Structurally separate from `app/security/api_keys.py` (customer API keys)
and `app/security/passwords.py` (user passwords) even though the hashing
primitive here matches api_keys.py's: a session token, like an API key, is
a high-entropy value this server generates itself -- never user-chosen --
so a fast hash (SHA-256) carries no brute-force risk; the entropy lives in
the 256-bit random token, not in a guessable keyspace. That is the exact
opposite case from `passwords.py`'s human-chosen secrets, which need a
deliberately slow KDF instead. Kept as its own module (not imported from
api_keys.py) so dashboard-session and customer-API-key code can evolve
independently -- see docs/decisions/008 and this phase's own investigation
for why the two authentication paths must stay fully separate.
"""

from __future__ import annotations

import hashlib
import secrets

_TOKEN_BYTES = 32  # 256 bits of entropy.


def generate_session_token() -> tuple[str, str]:
    """Generate a new session token. Returns (raw_token, token_hash).

    `raw_token` must be returned to the caller exactly once (at login) and
    is never persisted -- only `token_hash` is stored, on the
    `DashboardSession` row, mirroring `app.security.api_keys.
    generate_api_key`'s identical raw-vs-hash split.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    return raw_token, hash_session_token(raw_token)


def hash_session_token(raw_token: str) -> str:
    """Hash a presented raw session token for comparison against the stored
    `token_hash`. See the module docstring for why SHA-256 is appropriate
    here."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
