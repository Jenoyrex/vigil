"""`POST /v1/auth/login`, `POST /v1/auth/signup`, `POST /v1/auth/logout`,
`GET /v1/auth/session` -- dashboard user authentication.

**Entirely separate from customer API-key authentication.** These routes
authenticate a human using `apps/dashboard` by email/password and issue a
session token; they never accept, validate, or even look at a customer
`Authorization: Bearer vgl_*` key (`app.api.deps.get_current_api_key`).
The dashboard then presents that session token, plus the project being
viewed, to the read/config API (`app.api.deps.get_project_access`), which
authorizes it by organization membership -- so each user sees only their
own organizations' projects. Signup creates a user with no organization;
app/api/v1/workspace.py creates organizations, projects and API keys.

Layering matches every other route module in this API: rate limiting
(`app.api.rate_limit.require_login_rate_limit`, IP-keyed -- login has no
authenticated identity to key on any more than bootstrap does) ->
validation (`app.schemas.auth`) -> `app.services.auth` -> response.

**Login failures are always generic.** `POST /v1/auth/login` returns the
identical "Invalid email or password." for an unknown email, a wrong
password, or an inactive account
-- see `app.services.auth.authenticate_and_create_session`'s docstring for
how it also keeps response *timing* uniform across those cases, not only
the response body.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_session_token
from app.api.rate_limit import (
    RateLimiter,
    enforce_login_account_rate_limit,
    get_login_account_rate_limiter,
    require_login_rate_limit,
)
from app.config import settings
from app.db.session import get_db
from app.schemas.auth import LoginRequest, LoginResponse, SessionResponse, SignupRequest
from app.services.auth import (
    EmailAlreadyRegisteredError,
    authenticate_and_create_session,
    register_user_and_create_session,
    revoke_session,
    validate_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

_GENERIC_LOGIN_ERROR = "Invalid email or password."
_GENERIC_SESSION_ERROR = "Invalid or expired session."


def _unauthorized_login() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_LOGIN_ERROR)


def _unauthorized_session() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_SESSION_ERROR)


@router.post(
    "/v1/auth/login",
    response_model=LoginResponse,
    dependencies=[Depends(require_login_rate_limit)],
    summary="Log in a dashboard user, issuing a new session token",
    description=(
        "Always returns the identical generic 401 for an unknown email, a "
        "wrong password, or an inactive account -- never reveals "
        "which. `session_token` "
        "is shown exactly once, here, and cannot be retrieved again; only "
        "its hash is persisted."
    ),
    responses={
        401: {"description": "Invalid email or password."},
        429: {
            "description": (
                "Rate limit exceeded for login attempts (per client IP or per account). "
                "See the Retry-After header."
            )
        },
    },
)
def login(
    payload: LoginRequest,
    db: Session = Depends(get_db),
    account_limiter: RateLimiter[str] = Depends(get_login_account_rate_limiter),
) -> LoginResponse:
    enforce_login_account_rate_limit(account_limiter, payload.email)
    result = authenticate_and_create_session(
        db,
        email=payload.email,
        password=payload.password,
        session_ttl_hours=settings.dashboard_session_ttl_hours,
    )
    if result is None:
        raise _unauthorized_login()

    return LoginResponse(session_token=result.session_token, expires_at=result.expires_at)


@router.post(
    "/v1/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a dashboard session",
    description=(
        "Idempotent: a missing session token, an unknown token, or an "
        "already-revoked session all return 204 -- logout always "
        "'succeeds' from the caller's perspective. This deliberately never "
        "distinguishes those cases (there is nothing sensitive to protect "
        "by doing so, unlike login), so a client never needs special "
        "handling for 'log out when maybe already logged out.'"
    ),
)
def logout(
    session_token: str | None = Depends(get_session_token), db: Session = Depends(get_db)
) -> Response:
    if session_token is not None:
        revoke_session(db, raw_token=session_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/v1/auth/session",
    response_model=SessionResponse,
    summary="Validate a dashboard session",
    description=(
        "Called by apps/dashboard's proxy on every request to a gated "
        "route -- returns the session's owning user if `X-Vigil-Session-"
        "Token` is present, unexpired, unrevoked, and belongs to an active "
        "user; 401 otherwise. No caching layer sits in front of this on "
        "either side: a revoked session is rejected on its very next "
        "request."
    ),
    responses={401: {"description": "Invalid or expired session."}},
)
def get_session(
    session_token: str | None = Depends(get_session_token), db: Session = Depends(get_db)
) -> SessionResponse:
    if session_token is None:
        raise _unauthorized_session()

    result = validate_session(db, raw_token=session_token)
    if result is None:
        raise _unauthorized_session()

    return SessionResponse(user_id=result.user_id, email=result.email, expires_at=result.expires_at)


@router.post(
    "/v1/auth/signup",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_login_rate_limit)],
    summary="Create a dashboard account and log it in",
    description=(
        "Self-serve registration: creates a user with no organization yet "
        "(the dashboard's onboarding creates one next) and returns a new "
        "session exactly like login. Shares login's per-IP rate limit. "
        "Email verification is not implemented."
    ),
    responses={
        409: {"description": "An account with this email already exists."},
        422: {"description": "Invalid email, or password shorter than 12 characters."},
        429: {"description": "Rate limit exceeded. See the Retry-After header."},
    },
)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> LoginResponse:
    try:
        result = register_user_and_create_session(
            db,
            email=payload.email,
            password=payload.password,
            full_name=(payload.full_name or "").strip() or None,
            session_ttl_hours=settings.dashboard_session_ttl_hours,
        )
    except EmailAlreadyRegisteredError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Log in instead.",
        ) from None
    return LoginResponse(session_token=result.session_token, expires_at=result.expires_at)
