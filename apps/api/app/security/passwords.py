"""Password hashing for dashboard user authentication (Phase 4D, F1).

Uses Python's stdlib `hashlib.scrypt` -- a memory-hard KDF appropriate for
low-entropy, human-chosen secrets -- never the fast `hashlib.sha256` this
codebase uses for API keys (`app/security/api_keys.py`) or dashboard
session tokens (`app/security/sessions.py`): those are high-entropy,
server-generated random secrets, where a fast hash carries no brute-force
risk because the entropy lives entirely in the token. A user's password is
the opposite case -- chosen by a human from a comparatively small
practical keyspace -- so hashing it with a fast, unsalted-in-effort
algorithm would make offline brute-forcing a stolen `hashed_password`
value practical. `scrypt` deliberately costs real CPU/memory time per
guess; stdlib support means no new dependency is needed for this.

Encoded format: `scrypt$n=<N>$r=<R>$p=<P>$<salt_b64>$<hash_b64>` -- every
KDF parameter is carried in the stored string itself, not only in this
module's current constants, so a future parameter change (e.g. raising
`_DEFAULT_N` as hardware gets faster) never invalidates already-issued
hashes: verification always re-derives using the parameters recorded in
the hash being checked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_SCHEME = "scrypt"

# RFC 7914's recommended "interactive login" parameters. n is the CPU/memory
# cost factor (2**14 = 16384), r is block size, p is parallelization.
_DEFAULT_N = 2**14
_DEFAULT_R = 8
_DEFAULT_P = 1

_SALT_BYTES = 16
_DKLEN = 64


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage in `users.hashed_password`.

    Never logs `password`; callers must not log it either. A fresh random
    salt is generated on every call, so hashing the same password twice
    produces two different encoded strings.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_DEFAULT_N,
        r=_DEFAULT_R,
        p=_DEFAULT_P,
        dklen=_DKLEN,
    )
    return (
        f"{_SCHEME}$n={_DEFAULT_N}$r={_DEFAULT_R}$p={_DEFAULT_P}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(derived).decode('ascii')}"
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """Constant-time verification against a `hash_password`-produced string.

    Fails safe: any malformed `encoded_hash` (wrong scheme, unparseable
    parameters, corrupt base64, an out-of-range n/r/p) returns False rather
    than raising, so a corrupted or foreign-format value in the database
    can never crash a login attempt or surface via an exception path.
    """
    try:
        scheme, n_part, r_part, p_part, salt_b64, hash_b64 = encoded_hash.split("$")
        if scheme != _SCHEME:
            return False
        n = int(n_part.removeprefix("n="))
        r = int(r_part.removeprefix("r="))
        p = int(p_part.removeprefix("p="))
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except (ValueError, TypeError):
        return False

    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except ValueError:
        # n/r/p combination hashlib.scrypt rejects (e.g. absurd memory cost
        # from a corrupted hash) -- still a verification failure, not a crash.
        return False

    return hmac.compare_digest(derived, expected)


# A fixed-shape, valid-format hash not tied to any real user, used to keep
# login timing uniform whether or not the presented email matches a real
# account -- see app/services/auth.py's `authenticate_user`, which runs
# `verify_password` against this exact value when no user is found, paying
# the same scrypt cost either way so response time doesn't leak whether an
# account exists. Computed once per process from a random throwaway
# password -- never a real password, never persisted.
DUMMY_HASH_FOR_TIMING = hash_password(secrets.token_urlsafe(32))
