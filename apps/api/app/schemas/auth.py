"""Request/response schemas for `POST /v1/auth/login`, `POST /v1/auth/logout`,
`GET /v1/auth/session` (Phase 4D, F1) -- dashboard user authentication.

Entirely separate from `app/schemas/provisioning.py`'s bootstrap schemas
and from customer API-key authentication (`app.api.deps.get_current_api_key`):
these endpoints authenticate a human dashboard user by email/password and
issue a session token, and never accept or look at a customer `vgl_*` API
key.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

# Deliberately a simple, permissive shape check (not full RFC 5322
# compliance) -- the same "duplicate a small validator rather than couple
# unrelated schema modules together" precedent app/schemas/query.py's
# module docstring documents for TRACE_ID_RE/SPAN_ID_RE, applied here (this
# regex is intentionally re-declared, not imported, in
# app/schemas/provisioning.py). Real validation of "does this address
# work" happens by using it, not by a regex.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_email(value: str) -> str:
    normalized = value.strip().lower()
    if not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("must be a valid email address.")
    return normalized


Email = Annotated[str, AfterValidator(_validate_email)]


class LoginRequest(BaseModel):
    email: Email
    # No format/strength constraint here (unlike BootstrapRequest.
    # owner_password) -- this is a login attempt against an
    # already-created account, not account creation, so the only bound
    # needed is a sane upper limit against an absurdly large request body.
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    """`session_token` is the raw, unhashed token -- shown exactly once,
    here, and never again: only its SHA-256 hash is persisted
    (app/security/sessions.py), and there is no endpoint that can retrieve
    it later. The dashboard's own login route handler is expected to store
    it only in an HttpOnly cookie, never in JavaScript-accessible storage.
    """

    session_token: str
    expires_at: datetime


class SessionResponse(BaseModel):
    """Returned by `GET /v1/auth/session` for a valid, non-expired,
    non-revoked session belonging to an active user -- what
    `apps/dashboard/proxy.ts` checks on every gated request."""

    user_id: uuid.UUID
    email: str
    expires_at: datetime
