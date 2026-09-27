"""Business logic for dashboard user authentication (Phase 4D, F1):
`POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/session`.
Authentication (`app.api.deps.get_bootstrap_auth`-style header checks) and
rate limiting both happen before a request ever reaches here, matching
every other service module's layering in this codebase -- see
app/api/v1/auth.py.

Entirely separate from customer API-key authentication
(app.api.deps.get_current_api_key, app.security.api_keys) and from
bootstrap provisioning (app.services.provisioning) -- this module only
ever queries `users`/`organization_memberships`/`dashboard_sessions`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import DashboardSession, User
from app.security.passwords import DUMMY_HASH_FOR_TIMING, hash_password, verify_password
from app.security.sessions import generate_session_token, hash_session_token

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginResult:
    session_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: UUID
    user_id: UUID
    email: str
    expires_at: datetime


def authenticate_and_create_session(
    db: Session, *, email: str, password: str, session_ttl_hours: int
) -> LoginResult | None:
    """Verify email/password and, on success, issue a new session.

    Returns `None` for every failure case -- unknown email, wrong
    password, or inactive user --
    so the caller (app/api/v1/auth.py) always responds with an identical
    generic message regardless of which condition actually failed, never
    revealing which one it was.

    Always runs a real `scrypt` verification, even when no user is found
    (against `app.security.passwords.DUMMY_HASH_FOR_TIMING`), so response
    timing does not itself leak whether the presented email exists --
    a real user lookup + a real scrypt computation happen on every call,
    known-email or not.
    """
    user = db.query(User).filter(func.lower(User.email) == email.strip().lower()).first()

    password_hash = (
        user.hashed_password if user is not None and user.hashed_password else DUMMY_HASH_FOR_TIMING
    )
    password_ok = verify_password(password, password_hash)

    if user is None or not password_ok or not user.is_active:
        logger.info("dashboard login failed")
        return None

    # No organization membership is required: a newly signed-up user logs in
    # before creating one, and the dashboard routes them to onboarding.
    # Tenant data is still unreachable without membership -- every
    # project-scoped route checks it (app.api.deps.get_project_access).
    return _create_session(db, user_id=user.id, session_ttl_hours=session_ttl_hours)


class EmailAlreadyRegisteredError(Exception):
    """`register_user_and_create_session`: the email already has an account."""


def register_user_and_create_session(
    db: Session,
    *,
    email: str,
    password: str,
    full_name: str | None,
    session_ttl_hours: int,
) -> LoginResult:
    """Self-serve signup (`POST /v1/auth/signup`): create an active user with
    no organization yet, and log them in. The password is only ever stored
    as its scrypt hash. Raises `EmailAlreadyRegisteredError` for a taken
    email -- including one lost to a concurrent signup, which the
    `lower(email)` unique index catches."""
    normalized = email.strip().lower()
    if db.query(User.id).filter(func.lower(User.email) == normalized).first() is not None:
        raise EmailAlreadyRegisteredError
    user = User(email=normalized, full_name=full_name, hashed_password=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise EmailAlreadyRegisteredError from exc
    logger.info("dashboard user signed up", extra={"user_id": str(user.id)})
    return _create_session(db, user_id=user.id, session_ttl_hours=session_ttl_hours)


def _create_session(db: Session, *, user_id: UUID, session_ttl_hours: int) -> LoginResult:
    raw_token, token_hash = generate_session_token()
    expires_at = datetime.now(UTC) + timedelta(hours=session_ttl_hours)

    session = DashboardSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    db.add(session)
    db.commit()

    logger.info(
        "dashboard session created",
        extra={"user_id": str(user_id), "session_id": str(session.id)},
    )

    return LoginResult(session_token=raw_token, expires_at=expires_at)


def validate_session(db: Session, *, raw_token: str) -> AuthenticatedSession | None:
    """Resolve a presented raw session token to its owning user.

    Returns `None` if the token is unknown, expired, revoked, or its user
    is no longer active -- never raises for any of those; an invalid
    session is exactly as valid as no session at all, by design (see
    app/api/v1/auth.py's `GET /v1/auth/session`, which apps/dashboard's
    proxy.ts calls on every gated request).
    """
    token_hash = hash_session_token(raw_token)
    row = (
        db.query(DashboardSession, User)
        .join(User, User.id == DashboardSession.user_id)
        .filter(DashboardSession.token_hash == token_hash)
        .first()
    )
    if row is None:
        return None

    session, user = row
    now = datetime.now(UTC)
    if session.revoked_at is not None:
        return None
    if session.expires_at <= now:
        return None
    if not user.is_active:
        return None

    return AuthenticatedSession(
        session_id=session.id,
        user_id=user.id,
        email=user.email,
        expires_at=session.expires_at,
    )


def revoke_session(db: Session, *, raw_token: str) -> None:
    """Revoke the session matching `raw_token`, if any.

    Idempotent by design: a token that matches no row, or already matches
    a revoked session, is treated as success -- logout always "works" from
    the caller's perspective (see app/api/v1/auth.py's `POST
    /v1/auth/logout` docstring for why this is the documented, intended
    behavior, not a bug).
    """
    token_hash = hash_session_token(raw_token)
    session = db.query(DashboardSession).filter(DashboardSession.token_hash == token_hash).first()
    if session is None or session.revoked_at is not None:
        return
    session.revoked_at = datetime.now(UTC)
    db.commit()
