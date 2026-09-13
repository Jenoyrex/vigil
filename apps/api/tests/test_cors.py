"""CORS policy: explicit deny-by-default (Phase 4C). See
docs/decisions/007-cors-and-dashboard-security-headers.md.

Two levels of testing: an ISOLATED test app with a known, configured origin
allowlist (app.main.app's real CORSMiddleware configuration is baked in
from the real environment once, at module-import time, for the whole test
session -- there is no dependency-injection mechanism for ASGI middleware
the way there is for FastAPI route dependencies), proving the middleware
itself enforces allow/deny correctly; and the REAL app, proving its actual
deployed default (no configured origins) is genuinely deny-by-default.

Exact status codes/headers below (200 for an allowed/disallowed simple
request, 400 for a disallowed preflight, the precise header set on each)
were verified empirically against this exact fastapi/starlette version
before being asserted here, not assumed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(internal_service_token="test-internal-service-token", **overrides)


# -- Settings.cors_allowed_origins_list parsing/validation -----------------


def test_empty_string_parses_to_no_origins() -> None:
    assert _settings(cors_allowed_origins="").cors_allowed_origins_list == []


def test_single_origin_is_parsed() -> None:
    settings = _settings(cors_allowed_origins="https://app.example.com")
    assert settings.cors_allowed_origins_list == ["https://app.example.com"]


def test_multiple_origins_are_parsed_and_whitespace_trimmed() -> None:
    settings = _settings(
        cors_allowed_origins=" https://app.example.com , https://admin.example.com "
    )
    assert settings.cors_allowed_origins_list == [
        "https://app.example.com",
        "https://admin.example.com",
    ]


def test_wildcard_origin_is_rejected() -> None:
    settings = _settings(cors_allowed_origins="*")
    with pytest.raises(ValueError, match=r"must not contain '\*'"):
        _ = settings.cors_allowed_origins_list


def test_wildcard_among_other_origins_is_still_rejected() -> None:
    settings = _settings(cors_allowed_origins="https://app.example.com,*")
    with pytest.raises(ValueError, match=r"must not contain '\*'"):
        _ = settings.cors_allowed_origins_list


# -- middleware behavior, isolated test app with a known origin ------------


def _isolated_app(allowed_origins: list[str]) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/v1/traces")
    def _route() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_allowed_origin_receives_cors_header_on_a_simple_request() -> None:
    client = _isolated_app(["https://allowed.example.com"])
    response = client.get("/v1/traces", headers={"Origin": "https://allowed.example.com"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://allowed.example.com"


def test_unconfigured_origin_gets_no_cors_header_on_a_simple_request() -> None:
    client = _isolated_app(["https://allowed.example.com"])
    response = client.get("/v1/traces", headers={"Origin": "https://evil.example.com"})
    # The server still processes and returns the response (CORS is
    # enforced by the browser's own handling of the response, not the
    # server refusing to respond) -- what must be absent is the header
    # that would let a browser's JS read the response cross-origin.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_no_origin_header_at_all_is_unaffected() -> None:
    # A non-browser client (curl, the Python SDK, the worker's poller)
    # never sends an Origin header and is not subject to CORS at all --
    # CORS is a browser-only enforcement mechanism.
    client = _isolated_app(["https://allowed.example.com"])
    response = client.get("/v1/traces")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_preflight_from_an_allowed_origin_succeeds() -> None:
    client = _isolated_app(["https://allowed.example.com"])
    response = client.options(
        "/v1/traces",
        headers={
            "Origin": "https://allowed.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://allowed.example.com"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Authorization" in response.headers["access-control-allow-headers"]


def test_preflight_from_an_unconfigured_origin_is_rejected() -> None:
    client = _isolated_app(["https://allowed.example.com"])
    response = client.options(
        "/v1/traces",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_no_configured_origins_denies_every_cross_origin_request() -> None:
    # The genuinely deny-by-default case: zero configured origins.
    client = _isolated_app([])
    response = client.get("/v1/traces", headers={"Origin": "https://anything.example.com"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_no_configured_origins_rejects_every_preflight() -> None:
    client = _isolated_app([])
    response = client.options(
        "/v1/traces",
        headers={
            "Origin": "https://anything.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_cors_never_grants_credentialed_access() -> None:
    client = _isolated_app(["https://allowed.example.com"])
    response = client.get("/v1/traces", headers={"Origin": "https://allowed.example.com"})
    assert "access-control-allow-credentials" not in response.headers


# -- the REAL app's actual deployed default ---------------------------------


def test_real_app_default_configuration_is_deny_by_default() -> None:
    """Proves app.main.app's ACTUAL, deployed CORSMiddleware configuration
    -- built once at module-import time from the real environment -- is
    deny-by-default, not just the isolated test app above."""
    from app.config import settings as real_settings
    from app.main import app as real_app

    if real_settings.cors_allowed_origins_list:
        pytest.skip(
            "VIGIL_API_CORS_ALLOWED_ORIGINS is non-empty in this test "
            "environment; this test only proves the empty/default case."
        )

    plain_client = TestClient(real_app)
    response = plain_client.get("/health", headers={"Origin": "https://anything.example.com"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
