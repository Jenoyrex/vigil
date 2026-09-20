"""Client-IP attribution and per-account limiting for `POST /v1/auth/login`
(Phase 4D, F1 follow-up), and proof that bootstrap is unaffected.

Deliberately database-free: `authenticate_and_create_session` is stubbed to
"login failed" (the generic 401), so what these tests exercise is purely
which bucket each request is charged to. The database-backed login
behavior stays covered by tests/test_auth_api.py.

TestClient's simulated peer is always the literal host "testclient", which
is what "falls back to the direct peer" means below.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.deps import CLIENT_IP_HEADER, DASHBOARD_TOKEN_HEADER, get_login_client_key
from app.api.rate_limit import (
    RateLimiter,
    get_bootstrap_rate_limiter,
    get_login_account_rate_limiter,
    get_login_rate_limiter,
    normalize_login_email,
)
from app.config import settings
from app.db.session import get_db
from app.main import app

LOGIN_URL = "/v1/auth/login"
BOOTSTRAP_URL = "/v1/provisioning/bootstrap"
TOKEN = "test-dashboard-client-ip-token"

_STRICT = {"capacity": 1, "refill_per_second": 0.0001, "max_tracked_keys": 100}
_GENEROUS = {"capacity": 1000, "refill_per_second": 1000.0, "max_tracked_keys": 1000}


@pytest.fixture
def api() -> Iterator[TestClient]:
    """A TestClient with login authentication stubbed out and no database."""
    app.dependency_overrides[get_db] = lambda: None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.api.v1.auth.authenticate_and_create_session", lambda *a, **k: None)
        mp.setattr(settings, "dashboard_client_ip_token", TOKEN)
        yield TestClient(app)
    app.dependency_overrides.clear()


def _limiters(*, ip: dict, account: dict) -> None:
    ip_limiter: RateLimiter[str] = RateLimiter(**ip)
    account_limiter: RateLimiter[str] = RateLimiter(**account)
    app.dependency_overrides[get_login_rate_limiter] = lambda: ip_limiter
    app.dependency_overrides[get_login_account_rate_limiter] = lambda: account_limiter


def _login(api: TestClient, email: str = "a@example.com", **headers: str):
    return api.post(LOGIN_URL, json={"email": email, "password": "x"}, headers=headers)


def _trusted(ip: str, **extra: str) -> dict[str, str]:
    return {DASHBOARD_TOKEN_HEADER: TOKEN, CLIENT_IP_HEADER: ip, **extra}


def _request(peer: str | None = "172.18.0.5") -> Request:
    return Request(
        {
            "type": "http",
            "headers": [],
            "client": (peer, 40000) if peer is not None else None,
        }
    )


# -- get_login_client_key: which identity a login is charged to -------------


def test_trusted_dashboard_client_ip_is_used() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "dashboard_client_ip_token", TOKEN)
        assert get_login_client_key(_request(), TOKEN, "203.0.113.7") == "203.0.113.7"


def test_client_ip_is_normalized_so_equivalent_spellings_share_a_bucket() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "dashboard_client_ip_token", TOKEN)
        assert get_login_client_key(_request(), TOKEN, " 2001:DB8:0:0:0:0:0:1 ") == "2001:db8::1"


@pytest.mark.parametrize(
    ("token", "ip"),
    [
        (None, "203.0.113.7"),  # no credential
        ("wrong-token", "203.0.113.7"),  # wrong credential
        (TOKEN, None),  # credential but no IP reported
        (None, None),  # plain direct API traffic
    ],
)
def test_untrusted_or_missing_credential_falls_back_to_peer(token, ip) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "dashboard_client_ip_token", TOKEN)
        assert get_login_client_key(_request(), token, ip) == "172.18.0.5"


def test_disabled_feature_never_trusts_the_header_even_with_an_empty_token() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "dashboard_client_ip_token", "")
        assert get_login_client_key(_request(), "", "203.0.113.7") == "172.18.0.5"


@pytest.mark.parametrize(
    "bad_ip",
    ["not-an-ip", "999.1.1.1", "", "1.2.3.4, 5.6.7.8", "203.0.113.7\r\nX-Evil: 1", "1.2.3.4:80"],
)
def test_malformed_client_ip_falls_back_to_peer(bad_ip: str) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "dashboard_client_ip_token", TOKEN)
        assert get_login_client_key(_request(), TOKEN, bad_ip) == "172.18.0.5"


def test_non_ascii_token_is_rejected_not_a_server_error() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "dashboard_client_ip_token", TOKEN)
        assert get_login_client_key(_request(), "tökén", "203.0.113.7") == "172.18.0.5"


def test_missing_peer_falls_back_to_unknown() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "dashboard_client_ip_token", TOKEN)
        assert get_login_client_key(_request(peer=None), None, None) == "unknown"


# -- per-IP limiter, over HTTP ------------------------------------------------


def test_two_trusted_client_ips_have_independent_buckets(api: TestClient) -> None:
    _limiters(ip=_STRICT, account=_GENEROUS)

    assert _login(api, **_trusted("203.0.113.1")).status_code == 401
    assert _login(api, **_trusted("203.0.113.1")).status_code == 429
    # A different real user behind the same dashboard is unaffected.
    assert _login(api, **_trusted("203.0.113.2")).status_code == 401


def test_without_credential_the_reported_ip_is_ignored_and_peer_is_charged(
    api: TestClient,
) -> None:
    _limiters(ip=_STRICT, account=_GENEROUS)

    # No token: different reported IPs still land in the one peer bucket.
    assert _login(api, **{CLIENT_IP_HEADER: "203.0.113.1"}).status_code == 401
    assert _login(api, **{CLIENT_IP_HEADER: "203.0.113.2"}).status_code == 429


@pytest.mark.parametrize("spoof_header", ["X-Forwarded-For", "X-Real-IP", "Forwarded"])
def test_spoofed_forwarding_headers_cannot_change_attribution(
    api: TestClient, spoof_header: str
) -> None:
    _limiters(ip=_STRICT, account=_GENEROUS)

    # Direct traffic rotating a forged source address is still one peer.
    assert _login(api, **{spoof_header: "198.51.100.1"}).status_code == 401
    assert _login(api, **{spoof_header: "198.51.100.2"}).status_code == 429


def test_forwarding_headers_do_not_override_a_trusted_client_ip(api: TestClient) -> None:
    _limiters(ip=_STRICT, account=_GENEROUS)

    first = _login(api, **_trusted("203.0.113.1", **{"X-Forwarded-For": "198.51.100.1"}))
    second = _login(api, **_trusted("203.0.113.1", **{"X-Forwarded-For": "198.51.100.2"}))

    assert first.status_code == 401
    assert second.status_code == 429


def test_malformed_client_ip_over_http_is_charged_to_peer(api: TestClient) -> None:
    _limiters(ip=_STRICT, account=_GENEROUS)

    assert _login(api, **_trusted("not-an-ip")).status_code == 401
    assert _login(api, **_trusted("also-not-an-ip")).status_code == 429


# -- per-account limiter, over HTTP --------------------------------------------


def test_same_email_from_different_ips_shares_the_account_bucket(api: TestClient) -> None:
    _limiters(ip=_GENEROUS, account=_STRICT)

    assert _login(api, "victim@example.com", **_trusted("203.0.113.1")).status_code == 401
    second = _login(api, "victim@example.com", **_trusted("203.0.113.2"))

    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1


def test_different_emails_have_independent_account_buckets(api: TestClient) -> None:
    _limiters(ip=_GENEROUS, account=_STRICT)

    assert _login(api, "one@example.com").status_code == 401
    assert _login(api, "two@example.com").status_code == 401
    assert _login(api, "one@example.com").status_code == 429


def test_email_case_and_whitespace_normalize_to_one_bucket(api: TestClient) -> None:
    _limiters(ip=_GENEROUS, account=_STRICT)

    assert _login(api, "Owner@Example.com").status_code == 401
    assert _login(api, "  owner@example.COM  ").status_code == 429


def test_normalize_login_email() -> None:
    assert normalize_login_email("  Owner@Example.COM ") == "owner@example.com"
    assert normalize_login_email("owner@example.com") == "owner@example.com"


def test_unknown_email_is_rate_limited_like_any_other(api: TestClient) -> None:
    """The stub makes every account 'unknown'; nothing in the limiter
    consults whether an account exists, so the 429 arrives identically for
    accounts that do and don't exist (no existence side channel)."""
    _limiters(ip=_GENEROUS, account=_STRICT)

    first = _login(api, "ghost@example.com")
    second = _login(api, "ghost@example.com")

    assert first.status_code == 401
    assert second.status_code == 429
    assert second.json() == {
        "detail": f"Rate limit exceeded. Retry after {second.headers['Retry-After']} seconds."
    }


def test_malformed_email_is_rejected_and_still_charged_to_the_ip_limiter(api: TestClient) -> None:
    """A malformed email never reaches authentication (422), so it can't be
    used to guess anything -- but it still consumes the per-IP budget, so it
    can't be used to hammer the endpoint for free either."""
    _limiters(ip=_STRICT, account=_GENEROUS)

    first = _login(api, "not-an-email")
    second = _login(api, "not-an-email")

    assert first.status_code == 422
    assert second.status_code == 429


def test_ip_and_account_limits_are_independent_defenses(api: TestClient) -> None:
    _limiters(ip=_STRICT, account=_STRICT)

    assert _login(api, "a@example.com", **_trusted("203.0.113.1")).status_code == 401
    # Same IP, different account: the IP limiter still stops it.
    assert _login(api, "b@example.com", **_trusted("203.0.113.1")).status_code == 429
    # Different IP, same account: the account limiter stops it.
    assert _login(api, "a@example.com", **_trusted("203.0.113.9")).status_code == 429


def test_login_429_shape_and_retry_after(api: TestClient) -> None:
    _limiters(ip=_STRICT, account=_GENEROUS)

    _login(api)
    response = _login(api)

    assert response.status_code == 429
    retry_after = int(response.headers["Retry-After"])
    assert retry_after >= 1
    assert f"Retry after {retry_after} seconds" in response.json()["detail"]


# -- bootstrap is unchanged -----------------------------------------------------


def test_bootstrap_ignores_dashboard_client_ip_attribution(api: TestClient) -> None:
    bootstrap_limiter: RateLimiter[str] = RateLimiter(**_STRICT)
    app.dependency_overrides[get_bootstrap_rate_limiter] = lambda: bootstrap_limiter
    body = {
        "organization_name": "Acme",
        "project_name": "Prod",
        "owner_email": "owner@example.com",
        "owner_password": "correct horse battery staple",
    }

    # Even with a fully valid dashboard credential and distinct reported
    # IPs, both requests are charged to the one direct peer.
    first = api.post(BOOTSTRAP_URL, json=body, headers=_trusted("203.0.113.1"))
    second = api.post(BOOTSTRAP_URL, json=body, headers=_trusted("203.0.113.2"))

    assert first.status_code != 429  # bootstrap disabled -> 401, but it was admitted
    assert second.status_code == 429
