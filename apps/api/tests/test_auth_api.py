"""`POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/session` --
Phase 4D, F1. See app/api/v1/auth.py's module docstring for the full
design. Covers: generic login-failure responses (never revealing which
condition failed), session validation/expiry/revocation over HTTP, logout
idempotency, rate limiting, and that a customer API key can never
authenticate as a dashboard user (and vice versa).

Real PostgreSQL throughout (`db_session`/`client`, per tests/conftest.py)
-- no fakes/mocks for any database invariant.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.rate_limit import RateLimiter, get_login_rate_limiter
from app.main import app

LOGIN_URL = "/v1/auth/login"
LOGOUT_URL = "/v1/auth/logout"
SESSION_URL = "/v1/auth/session"
SESSION_HEADER = "X-Vigil-Session-Token"

GENERIC_LOGIN_ERROR = "Invalid email or password."
GENERIC_SESSION_ERROR = "Invalid or expired session."


@pytest.fixture(autouse=True)
def _generous_login_rate_limit() -> None:
    """Same rationale as test_provisioning.py's identical fixture for the
    bootstrap limiter: `_login_limiter` is a process-wide singleton keyed
    by client IP, and TestClient always reports the same simulated IP, so
    every test in this module sharing the real (production-sized) limiter
    would exhaust it and start seeing spurious 429s unrelated to what a
    given test is actually checking. `tests/conftest.py`'s `client`
    fixture clears `app.dependency_overrides` after every test.
    """
    limiter: RateLimiter[str] = RateLimiter(
        capacity=1000, refill_per_second=1000.0, max_tracked_keys=10
    )
    app.dependency_overrides[get_login_rate_limiter] = lambda: limiter


def _override_login_limiter(**kwargs) -> RateLimiter:
    limiter = RateLimiter(**kwargs)
    app.dependency_overrides[get_login_rate_limiter] = lambda: limiter
    return limiter


# -- login: success -----------------------------------------------------


def test_login_succeeds_with_correct_credentials(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    response = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"session_token", "expires_at"}
    assert body["session_token"]


def test_login_email_is_case_insensitive(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    response = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email.upper(),
            "password": active_dashboard_user.raw_password,
        },
    )
    assert response.status_code == 200


# -- login: generic failures ---------------------------------------------


def test_login_unknown_email_is_generic_401(client: TestClient) -> None:
    response = client.post(
        LOGIN_URL, json={"email": "nobody@example.com", "password": "whatever"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_LOGIN_ERROR


def test_login_wrong_password_is_generic_401(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    response = client.post(
        LOGIN_URL,
        json={"email": active_dashboard_user.user.email, "password": "definitely wrong"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_LOGIN_ERROR


def test_login_inactive_user_is_generic_401(
    client: TestClient, db_session, active_dashboard_user: SimpleNamespace
) -> None:
    active_dashboard_user.user.is_active = False
    db_session.commit()

    response = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_LOGIN_ERROR


def test_login_failure_responses_are_identical_across_failure_reasons(
    client: TestClient, db_session, active_dashboard_user: SimpleNamespace
) -> None:
    """The exact requirement: unknown email, wrong password, and an
    inactive account must be indistinguishable from the response alone."""
    unknown_email = client.post(
        LOGIN_URL, json={"email": "nobody@example.com", "password": "whatever"}
    )
    wrong_password = client.post(
        LOGIN_URL,
        json={"email": active_dashboard_user.user.email, "password": "definitely wrong"},
    )
    active_dashboard_user.user.is_active = False
    db_session.commit()
    inactive = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )

    for response in (unknown_email, wrong_password, inactive):
        assert response.status_code == 401
        assert response.json() == {"detail": GENERIC_LOGIN_ERROR}


def test_login_missing_fields_is_422(client: TestClient) -> None:
    response = client.post(LOGIN_URL, json={"email": "someone@example.com"})
    assert response.status_code == 422


def test_login_malformed_email_is_422(client: TestClient) -> None:
    response = client.post(LOGIN_URL, json={"email": "not-an-email", "password": "whatever"})
    assert response.status_code == 422


# -- session validation ---------------------------------------------------


def test_session_valid_token_returns_user_info(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    login = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )
    token = login.json()["session_token"]

    response = client.get(SESSION_URL, headers={SESSION_HEADER: token})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == active_dashboard_user.user.email
    assert body["user_id"] == str(active_dashboard_user.user.id)


def test_session_missing_header_is_generic_401(client: TestClient) -> None:
    response = client.get(SESSION_URL)
    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_SESSION_ERROR


def test_session_unknown_token_is_generic_401(client: TestClient) -> None:
    response = client.get(SESSION_URL, headers={SESSION_HEADER: "not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_SESSION_ERROR


def test_session_revoked_token_is_generic_401(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    login = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )
    token = login.json()["session_token"]
    client.post(LOGOUT_URL, headers={SESSION_HEADER: token})

    response = client.get(SESSION_URL, headers={SESSION_HEADER: token})

    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_SESSION_ERROR


def test_session_expired_token_is_generic_401(
    client: TestClient, db_session, active_dashboard_user: SimpleNamespace
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db.models import DashboardSession
    from app.security.sessions import generate_session_token

    raw_token, token_hash = generate_session_token()
    db_session.add(
        DashboardSession(
            user_id=active_dashboard_user.user.id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    db_session.commit()

    response = client.get(SESSION_URL, headers={SESSION_HEADER: raw_token})

    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_SESSION_ERROR


# -- logout ----------------------------------------------------------------


def test_logout_revokes_the_session(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    login = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )
    token = login.json()["session_token"]

    logout_response = client.post(LOGOUT_URL, headers={SESSION_HEADER: token})
    assert logout_response.status_code == 204

    session_check = client.get(SESSION_URL, headers={SESSION_HEADER: token})
    assert session_check.status_code == 401


def test_logout_clears_no_body(client: TestClient, active_dashboard_user: SimpleNamespace) -> None:
    login = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )
    token = login.json()["session_token"]

    response = client.post(LOGOUT_URL, headers={SESSION_HEADER: token})
    assert response.status_code == 204
    assert response.content == b""


def test_logout_with_no_token_is_idempotent_204(client: TestClient) -> None:
    response = client.post(LOGOUT_URL)
    assert response.status_code == 204


def test_logout_with_unknown_token_is_idempotent_204(client: TestClient) -> None:
    response = client.post(LOGOUT_URL, headers={SESSION_HEADER: "not-a-real-token"})
    assert response.status_code == 204


def test_logout_twice_is_idempotent_204(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    login = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )
    token = login.json()["session_token"]

    first = client.post(LOGOUT_URL, headers={SESSION_HEADER: token})
    second = client.post(LOGOUT_URL, headers={SESSION_HEADER: token})

    assert first.status_code == 204
    assert second.status_code == 204


# -- separation from customer API-key authentication ------------------------


def test_customer_api_key_cannot_authenticate_as_dashboard_session(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    """A real, active, valid customer vgl_* key must never satisfy
    GET /v1/auth/session -- presented in the session-token header, it is
    just an arbitrary wrong string (not a real session token's hash), and
    presented as Authorization: Bearer (its normal home), this endpoint
    never even looks at that header."""
    as_session_token = client.get(SESSION_URL, headers={SESSION_HEADER: active_api_key.raw_key})
    assert as_session_token.status_code == 401
    assert as_session_token.json()["detail"] == GENERIC_SESSION_ERROR

    as_bearer_only = client.get(
        SESSION_URL, headers={"Authorization": f"Bearer {active_api_key.raw_key}"}
    )
    assert as_bearer_only.status_code == 401


def test_dashboard_session_cannot_authenticate_customer_endpoint(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    """The reverse direction: a real dashboard session token must never
    satisfy a customer-API-key-protected endpoint, presented either as a
    Bearer token (its normal customer-key home) or in the session header
    (which that endpoint never reads)."""
    login = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )
    token = login.json()["session_token"]

    as_bearer = client.get(
        "/v1/evaluations/configs", headers={"Authorization": f"Bearer {token}"}
    )
    assert as_bearer.status_code == 401


# -- rate limiting -----------------------------------------------------------


def test_login_rate_limit_returns_429_with_retry_after(client: TestClient) -> None:
    _override_login_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)

    first = client.post(LOGIN_URL, json={"email": "nobody@example.com", "password": "x"})
    second = client.post(LOGIN_URL, json={"email": "nobody@example.com", "password": "x"})

    assert first.status_code == 401  # consumed a token, still a normal generic failure
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert int(second.headers["Retry-After"]) >= 1


def test_login_rate_limit_applies_regardless_of_correct_or_wrong_credentials(
    client: TestClient, active_dashboard_user: SimpleNamespace
) -> None:
    """The rate limiter must throttle by IP regardless of whether the
    attempt would have succeeded -- otherwise an attacker could tell
    "right password, rate limited" apart from "wrong password, rate
    limited", a side channel this generic-error design must not leak."""
    _override_login_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)

    first = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )
    second = client.post(
        LOGIN_URL,
        json={
            "email": active_dashboard_user.user.email,
            "password": active_dashboard_user.raw_password,
        },
    )

    assert first.status_code == 200
    assert second.status_code == 429
