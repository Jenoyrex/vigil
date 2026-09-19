"""`POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/session`
(Phase 4D, F1) -- dashboard user authentication.

**Entirely separate from customer API-key authentication.** These routes
authenticate a human logging into `apps/dashboard` by email/password and
issue a session token; they never accept, validate, or even look at a
customer `Authorization: Bearer vgl_*` key (`app.api.deps.
get_current_api_key`). `apps/dashboard`'s own server-held project API key
(`VIGIL_API_KEY`) continues to authenticate every BFF-to-API telemetry
call exactly as before, completely independent of whether -- or which --
dashboard user is logged in; see this phase's investigation report for why
that separation is the correct architecture for Vigil's current
one-project-per-deployment model, not a shortcut.

Layering matches every other route module in this API: rate limiting
(`app.api.rate_limit.require_login_rate_limit`, IP-keyed -- login has no
authenticated identity to key on any more than bootstrap does) ->
validation (`app.schemas.auth`) -> `app.services.auth` -> response.

**Login failures are always generic.** `POST /v1/auth/login` returns the
identical "Invalid email or password." for an unknown email, a wrong
password, an inactive account, or a user with no organization membership
-- see `app.services.auth.authenticate_and_create_session`'s docstring for
how it also keeps response *timing* uniform across those cases, not only
the response body.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_session_token
from app.api.rate_limit import require_login_rate_limit
from app.config import settings
from app.db.session import get_db
from app.schemas.auth import LoginRequest, LoginResponse, SessionResponse
from app.services.auth import authenticate_and_create_session, revoke_session, validate_session

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
        "wrong password, an inactive account, or a user with no "
        "organization membership -- never reveals which. `session_token` "
        "is shown exactly once, here, and cannot be retrieved again; only "
        "its hash is persisted."
    ),
    responses={
        401: {"description": "Invalid email or password."},
        429: {"description": "Rate limit exceeded for login attempts. See the Retry-After header."},
    },
)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
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
