"""Shared FastAPI dependencies for API routes: authentication and DB access."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, update
from sqlalchemy.orm import Session

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
