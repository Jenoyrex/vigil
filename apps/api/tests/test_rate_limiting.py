"""API-level rate-limiting tests: HTTP 429 shape, Retry-After, tier
assignment, the health/ready/internal-endpoint exclusions, and auth
ordering. See test_rate_limiter.py for the token-bucket algorithm's own
unit tests (refill math, jitter-free Retry-After, concurrency, eviction).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.api.rate_limit import RateLimiter, get_default_rate_limiter, get_ingestion_rate_limiter
from app.main import app
from helpers import valid_traces_payload


def _override_ingestion_limiter(**kwargs) -> RateLimiter:
    limiter = RateLimiter(**kwargs)
    app.dependency_overrides[get_ingestion_rate_limiter] = lambda: limiter
    return limiter


def _override_default_limiter(**kwargs) -> RateLimiter:
    limiter = RateLimiter(**kwargs)
    app.dependency_overrides[get_default_rate_limiter] = lambda: limiter
    return limiter


def _auth_headers(api_key: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key.raw_key}"}


# -- tier assignment -----------------------------------------------------


def test_traces_ingestion_is_rate_limited_by_ingestion_tier(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    _override_ingestion_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)
    headers = _auth_headers(active_api_key)

    first = client.post("/v1/traces", json=valid_traces_payload(), headers=headers)
    assert first.status_code == 200

    second = client.post("/v1/traces", json=valid_traces_payload(), headers=headers)
    assert second.status_code == 429


def test_customer_read_endpoint_is_rate_limited_by_default_tier(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    _override_default_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)
    headers = _auth_headers(active_api_key)

    first = client.get("/v1/evaluations/configs", headers=headers)
    assert first.status_code == 200

    second = client.get("/v1/evaluations/configs", headers=headers)
    assert second.status_code == 429


# -- 429 response shape ----------------------------------------------------


def test_429_response_body_shape(client: TestClient, active_api_key: SimpleNamespace) -> None:
    _override_ingestion_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)
    headers = _auth_headers(active_api_key)

    client.post("/v1/traces", json=valid_traces_payload(), headers=headers)
    response = client.post("/v1/traces", json=valid_traces_payload(), headers=headers)

    assert response.status_code == 429
    body = response.json()
    assert set(body.keys()) == {"detail"}
    assert body["detail"].startswith("Rate limit exceeded. Retry after ")
    assert body["detail"].endswith(" seconds.")


def test_429_includes_a_valid_retry_after_header(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    _override_ingestion_limiter(capacity=1, refill_per_second=0.5, max_tracked_keys=10)
    headers = _auth_headers(active_api_key)

    client.post("/v1/traces", json=valid_traces_payload(), headers=headers)
    response = client.post("/v1/traces", json=valid_traces_payload(), headers=headers)

    assert response.status_code == 429
    retry_after = response.headers["retry-after"]
    assert retry_after.isdigit()
    assert int(retry_after) >= 1


# -- exclusions ------------------------------------------------------------


def test_health_endpoint_is_not_rate_limited() -> None:
    plain_client = TestClient(app)
    for _ in range(100):
        response = plain_client.get("/health")
        assert response.status_code == 200


def test_ready_endpoint_is_not_rate_limited() -> None:
    plain_client = TestClient(app)
    fake_ch_client = MagicMock()
    fake_ch_client.ping.return_value = None
    with (
        patch("app.main.get_clickhouse_client", return_value=fake_ch_client),
        patch("app.main.ping_database", return_value=None),
    ):
        for _ in range(100):
            response = plain_client.get("/ready")
            assert response.status_code == 200


def test_internal_evaluation_job_endpoint_is_not_customer_rate_limited(
    client: TestClient,
) -> None:
    # Deliberately no rate-limiter override here: POST /v1/evaluations/jobs
    # does not depend on require_ingestion_rate_limit or
    # require_default_rate_limit at all (only get_internal_service_auth,
    # unchanged by this commit), so it cannot be affected by either tier
    # regardless of their configured capacity.
    headers = {"X-Vigil-Internal-Token": "test-internal-service-token"}
    payload = {
        "project_id": str(uuid.uuid4()),
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "00f067aa0ba902b7",
        "evaluator_name": "relevance",
        "evaluator_version": "0.1.0",
    }

    statuses = set()
    for _ in range(10):
        response = client.post("/v1/evaluations/jobs", json=payload, headers=headers)
        statuses.add(response.status_code)

    assert 429 not in statuses


# -- per-key isolation -------------------------------------------------------


def test_different_api_keys_have_independent_ingestion_buckets(
    client: TestClient, active_api_key: SimpleNamespace, db_session
) -> None:
    from app.security.api_keys import generate_api_key
    from test_models import make_api_key, make_organization, make_project

    _override_ingestion_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)

    other_org = make_organization(db_session)
    other_project = make_project(db_session, other_org)
    raw_key, key_prefix, key_hash = generate_api_key()
    make_api_key(db_session, other_project, key_prefix=key_prefix, key_hash=key_hash)
    other_key = SimpleNamespace(raw_key=raw_key)

    first = client.post(
        "/v1/traces", json=valid_traces_payload(), headers=_auth_headers(active_api_key)
    )
    assert first.status_code == 200

    exhausted = client.post(
        "/v1/traces", json=valid_traces_payload(), headers=_auth_headers(active_api_key)
    )
    assert exhausted.status_code == 429

    # A completely different API key must have its own, unexhausted budget
    # -- proves buckets are keyed by api_key_id, not shared globally.
    other = client.post(
        "/v1/traces", json=valid_traces_payload(), headers=_auth_headers(other_key)
    )
    assert other.status_code == 200


def test_same_api_key_shares_one_bucket_across_requests(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    _override_ingestion_limiter(capacity=2, refill_per_second=0.0001, max_tracked_keys=10)
    headers = _auth_headers(active_api_key)

    for _ in range(2):
        response = client.post("/v1/traces", json=valid_traces_payload(), headers=headers)
        assert response.status_code == 200

    # The 3rd request with the SAME key must see the bucket already
    # partially consumed by the first two, not a fresh one.
    third = client.post("/v1/traces", json=valid_traces_payload(), headers=headers)
    assert third.status_code == 429


# -- auth/rate-limit ordering -------------------------------------------------


def test_missing_api_key_is_401_not_429(client: TestClient) -> None:
    _override_ingestion_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)

    response = client.post("/v1/traces", json=valid_traces_payload())

    assert response.status_code == 401


def test_invalid_api_key_is_401_even_with_an_exhausted_limiter(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    _override_ingestion_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)

    # Exhaust the VALID key's budget first.
    client.post(
        "/v1/traces", json=valid_traces_payload(), headers=_auth_headers(active_api_key)
    )

    # An unrelated, invalid key must still get 401 -- an unauthenticated
    # caller can never be rate-limited (there is no api_key_id to key a
    # bucket by), and can never be confused with a real, exhausted key.
    response = client.post(
        "/v1/traces",
        json=valid_traces_payload(),
        headers={"Authorization": "Bearer vgl_deadbeef.not-a-real-secret"},
    )
    assert response.status_code == 401


def test_revoked_api_key_is_401_not_429(
    client: TestClient, revoked_api_key: SimpleNamespace
) -> None:
    _override_ingestion_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)

    response = client.post(
        "/v1/traces", json=valid_traces_payload(), headers=_auth_headers(revoked_api_key)
    )
    assert response.status_code == 401


# -- ordinary usage is unaffected ---------------------------------------------


def test_ordinary_requests_under_the_configured_default_limit_are_unaffected(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    # Uses the REAL, configured default-tier limiter -- no override -- to
    # prove the actual production defaults don't interfere with a handful
    # of ordinary requests using one key.
    headers = _auth_headers(active_api_key)
    for _ in range(3):
        response = client.get("/v1/evaluations/configs", headers=headers)
        assert response.status_code == 200


def test_ordinary_ingestion_requests_under_the_configured_limit_are_unaffected(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    # Same, for the (stricter) ingestion tier's real configured default.
    headers = _auth_headers(active_api_key)
    for _ in range(3):
        response = client.post("/v1/traces", json=valid_traces_payload(), headers=headers)
        assert response.status_code == 200
