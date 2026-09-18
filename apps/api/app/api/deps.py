"""Shared FastAPI dependencies for API routes: authentication and DB access."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import APIKey
from app.db.session import get_db
from app.security.api_keys import has_expected_key_shape, hash_api_key

# auto_error=False so we control the error response ourselves: FastAPI's
# HTTPBearer defaults to 403 on missing/malformed credentials, but ADR-driven
# API design here requires 401 for any missing/invalid/revoked credential.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Vigil API key, formatted `vgl_<prefix>.<secret>`.",
)

_INVALID_KEY_DETAIL = "Invalid or missing API key."
_REVOKED_KEY_DETAIL = "This API key has been revoked."


@dataclass(frozen=True)
class AuthenticatedKey:
    """The result of successful API-key authentication.

    `project_id` is the ONLY source of tenant scoping for a request -- it is
    always resolved server-side from the authenticated key, and a request
    body's own `project_id` (if a client sends one) must never be trusted.
    """

    api_key_id: uuid.UUID
    project_id: uuid.UUID


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedKey:
    """Authenticate a request via `Authorization: Bearer <api-key>`.

    Steps (per docs/decisions/003-clickhouse-telemetry-storage.md's
    server-derived project_id requirement):
    1. Extract the bearer token (missing/malformed header -> 401).
    2. Cheaply check the token's shape before doing any hashing/DB work.
    3. Hash the presented key and look it up by `key_hash` (the column
       already carries a unique index, so this is a single indexed lookup).
    4. Reject a key that doesn't exist, or exists but isn't `active`.
    5. Update `last_used_at` (server-computed, best-effort) and return the
       resolved `project_id`.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized(_INVALID_KEY_DETAIL)

    raw_key = credentials.credentials
    if not has_expected_key_shape(raw_key):
        raise _unauthorized(_INVALID_KEY_DETAIL)

    key_hash = hash_api_key(raw_key)
    row = db.query(APIKey).filter(APIKey.key_hash == key_hash).first()

    if row is None:
        raise _unauthorized(_INVALID_KEY_DETAIL)
    if row.status != "active":
        raise _unauthorized(_REVOKED_KEY_DETAIL)

    db.execute(update(APIKey).where(APIKey.id == row.id).values(last_used_at=func.now()))
    db.commit()

    return AuthenticatedKey(api_key_id=row.id, project_id=row.project_id)


INTERNAL_TOKEN_HEADER = "X-Vigil-Internal-Token"

_INVALID_INTERNAL_TOKEN_DETAIL = "Invalid or missing internal service token."


def get_internal_service_auth(
    x_vigil_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
) -> None:
    """Authenticate the internal worker fleet via a single shared secret --
    docs/decisions/005-evaluation-job-storage-worker.md section 9, Phase 3H
    amendment. Used solely by `POST /v1/evaluations/jobs`.

    Structurally separate from `get_current_api_key` at every level, not
    merely a different value: a dedicated header (`X-Vigil-Internal-Token`,
    never `Authorization: Bearer`), compared with `hmac.compare_digest`
    (constant-time) against `settings.internal_service_token`, and **never
    touches the `api_keys` table at all**. This proves only "this caller is
    the trusted internal worker process" -- it carries zero project scope
    and returns nothing, unlike `AuthenticatedKey`. A customer's `vgl_*` key
    cannot satisfy this check: it would have to be presented in this exact
    header and match this exact secret byte-for-byte, which no `api_keys`
    row ever does (this dependency doesn't even look).
    """
    if x_vigil_internal_token is None or not hmac.compare_digest(
        x_vigil_internal_token, settings.internal_service_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_INTERNAL_TOKEN_DETAIL
        )


BOOTSTRAP_TOKEN_HEADER = "X-Vigil-Bootstrap-Token"

_INVALID_BOOTSTRAP_TOKEN_DETAIL = "Invalid or missing bootstrap authorization."


def get_bootstrap_auth(
    x_vigil_bootstrap_token: str | None = Header(default=None, alias=BOOTSTRAP_TOKEN_HEADER),
) -> None:
    """Authenticate `POST /v1/provisioning/bootstrap` (Phase 4D, F3) --
    structurally identical to `get_internal_service_auth` immediately above
    (a dedicated header, never `Authorization: Bearer`, compared with
    `hmac.compare_digest` against a dedicated `Settings` field, never
    touching the `api_keys` table), for the identical reason: this must be
    a wholly separate trust boundary from customer API-key authentication,
    not a variant of it. A customer's `vgl_*` key cannot satisfy this check
    no matter how it's presented.

    **Fails closed when unconfigured.** `settings.bootstrap_secret` is
    empty by default (see app/config.py) -- checked FIRST, before looking
    at the presented token at all, so an operator who has not explicitly
    opted in by setting a real secret gets an unconditional 401 for every
    request, including one presenting an empty token that would otherwise
    trivially "match" an empty configured secret. This is what makes
    bootstrap unreachable by default rather than merely undocumented.
    """
    if not settings.bootstrap_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_BOOTSTRAP_TOKEN_DETAIL
        )
    if x_vigil_bootstrap_token is None or not hmac.compare_digest(
        x_vigil_bootstrap_token, settings.bootstrap_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_BOOTSTRAP_TOKEN_DETAIL
        )


SESSION_TOKEN_HEADER = "X-Vigil-Session-Token"


def get_session_token(
    x_vigil_session_token: str | None = Header(default=None, alias=SESSION_TOKEN_HEADER),
) -> str | None:
    """Extracts a presented dashboard session token for `GET /v1/auth/session`
    and `POST /v1/auth/logout` (Phase 4D, F1) -- a dedicated header, never
    `Authorization: Bearer`, structurally identical in spirit to
    `get_internal_service_auth`/`get_bootstrap_auth` above: a wholly
    separate trust boundary from customer API-key authentication. A
    customer's `vgl_*` key presented in this header authenticates nothing
    -- this dependency doesn't look at the `api_keys` table at all, and
    `app.services.auth.validate_session`/`revoke_session` only ever
    compare against `dashboard_sessions.token_hash`.

    Deliberately does not itself raise on a missing/invalid token (unlike
    `get_bootstrap_auth`/`get_internal_service_auth`): the two callers need
    different behavior on "no token presented" -- `GET /v1/auth/session`
    must still return its generic 401, while `POST /v1/auth/logout` treats
    it as an already-logged-out no-op (see that route's own docstring) --
    so each decides that for itself from the plain `str | None` this
    returns.
    """
    return x_vigil_session_token
