"""End-to-end tests against a real local ClickHouse (see
infrastructure/clickhouse/) for the read-side Trace Explorer/analytics API:
ingest real spans via POST /v1/traces, then read them back through all five
GET endpoints with no mocking of the storage layer.

Skipped automatically if ClickHouse isn't reachable, so the rest of the
suite (and CI environments without ClickHouse running) aren't blocked by it.
Reuses the POST-retry-until-visible helper from
test_traces_clickhouse_integration.py (see that module for why it exists --
a local client/transport flakiness, not an application defect) but declares
its own `real_clickhouse_client`/`real_client` fixtures rather than
importing pytest fixtures across test modules by name, which ruff's static
analysis (correctly, if inconveniently) can't distinguish from an unused
import once more than one test function in the importing module re-declares
the same parameter name.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.clickhouse.client import get_clickhouse_client
from app.clickhouse.repository import SpansRepository
from app.config import settings
from helpers import valid_span, valid_traces_payload
from test_traces_clickhouse_integration import _post_until_visible


def _recent_span_times(offset_seconds: float = 0.0) -> tuple[str, str]:
    """`helpers.valid_span`'s own default start_time/end_time
    (2026-01-01T00:00:00Z) predate this test's default 24h query window
    relative to "now" -- these tests need spans that actually fall inside
    it, so every span here gets an explicit, current timestamp instead.
    """
    start = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    end = start + timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


@pytest.fixture
def real_clickhouse_client():
    try:
        ch_client = get_clickhouse_client()
        ch_client.ping()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"ClickHouse not reachable at {settings.clickhouse_host}:"
            f"{settings.clickhouse_port} ({exc}); start it via "
            "infrastructure/docker-compose.yml to run this test."
        )
    return ch_client


@pytest.fixture
def real_client(db_session: Session, real_clickhouse_client) -> Generator[TestClient, None, None]:
    from app.api.v1.analytics import get_analytics_repository
    from app.api.v1.traces import get_spans_repository, get_traces_query_repository
    from app.clickhouse.analytics_repository import AnalyticsRepository
    from app.clickhouse.query_repository import TracesQueryRepository
    from app.db.session import get_db
    from app.main import app

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_spans_repository] = lambda: SpansRepository(
        real_clickhouse_client
    )
    app.dependency_overrides[get_traces_query_repository] = lambda: TracesQueryRepository(
        real_clickhouse_client
    )
    app.dependency_overrides[get_analytics_repository] = lambda: AnalyticsRepository(
        real_clickhouse_client
    )
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_list_detail_span_and_analytics_against_real_clickhouse(
    real_client: TestClient,
    real_clickhouse_client,
    active_api_key: SimpleNamespace,
) -> None:
    trace_id = "ef" * 16
    root_span_id = "11" * 8
    child_span_id = "22" * 8
    headers = {"Authorization": f"Bearer {active_api_key.raw_key}"}

    root_start, root_end = _recent_span_times()
    child_start, child_end = _recent_span_times(offset_seconds=0.1)
    payload = valid_traces_payload(
        spans=[
            valid_span(
                trace_id=trace_id,
                span_id=root_span_id,
                parent_span_id=None,
                name="integration-root",
                span_type="agent",
                status="ok",
                start_time=root_start,
                end_time=root_end,
            ),
            valid_span(
                trace_id=trace_id,
                span_id=child_span_id,
                parent_span_id=root_span_id,
                name="integration-llm-call",
                span_type="llm",
                status="ok",
                start_time=child_start,
                end_time=child_end,
                llm_provider="openai",
                llm_model="gpt-4o-mini",
                llm_input_tokens=10,
                llm_output_tokens=5,
                llm_total_tokens=15,
                llm_cost_usd=0.0002,
                environment="integration-test",
            ),
        ]
    )

    ingest_response = _post_until_visible(
        real_client,
        real_clickhouse_client,
        payload=payload,
        headers=headers,
        project_id=active_api_key.project.id,
        trace_id=trace_id,
        span_id=root_span_id,
    )
    assert ingest_response.status_code == 200
    assert ingest_response.json()["accepted"] == 2

    # -- GET /v1/traces (list) -- default (last 24h) window ---------------
    list_response = real_client.get("/v1/traces", headers=headers)
    assert list_response.status_code == 200
    traces = list_response.json()["traces"]
    assert len(traces) == 1
    [trace_summary] = traces
    assert trace_summary["trace_id"] == trace_id
    assert trace_summary["status"] == "ok"
    assert trace_summary["span_count"] == 2
    assert trace_summary["root_span_name"] == "integration-root"

    # -- GET /v1/traces?environment=... -- regression test for a real
    # ClickHouse-only bug (ILLEGAL_AGGREGATION: "Aggregate function
    # any(environment) AS environment is found in WHERE") that only
    # manifests against a real server, never against the fake ClickHouse
    # client every other list_traces test uses -- see
    # app/clickhouse/query_repository.py's list_traces docstring.
    filtered_response = real_client.get(
        "/v1/traces", params={"environment": "integration-test"}, headers=headers
    )
    assert filtered_response.status_code == 200

    # -- GET /v1/traces/{trace_id} (detail) -------------------------------
    detail_response = real_client.get(f"/v1/traces/{trace_id}", headers=headers)
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["status"] == "ok"
    assert detail["total_span_count"] == 2
    assert detail["truncated"] is False
    span_ids = {span["span_id"] for span in detail["spans"]}
    assert span_ids == {root_span_id, child_span_id}
    child = next(s for s in detail["spans"] if s["span_id"] == child_span_id)
    assert child["parent_span_id"] == root_span_id
    assert child["llm_cost_usd"] == "0.000200"

    # -- GET /v1/traces/{trace_id}/spans/{span_id} (span detail) ----------
    span_response = real_client.get(f"/v1/traces/{trace_id}/spans/{child_span_id}", headers=headers)
    assert span_response.status_code == 200
    span = span_response.json()
    assert span["llm_provider"] == "openai"
    assert span["llm_total_tokens"] == 15

    # -- 404 for a trace_id that doesn't exist in this project -------------
    missing_response = real_client.get(f"/v1/traces/{'0' * 32}", headers=headers)
    assert missing_response.status_code == 404

    # -- GET /v1/analytics/spans -- default (last 24h) window -------------
    span_analytics_response = real_client.get("/v1/analytics/spans", headers=headers)
    assert span_analytics_response.status_code == 200
    span_analytics = span_analytics_response.json()
    assert span_analytics["span_count"] == 2
    assert span_analytics["error_span_count"] == 0

    # -- GET /v1/analytics/llm-usage -- default (last 24h) window ---------
    llm_usage_response = real_client.get("/v1/analytics/llm-usage", headers=headers)
    assert llm_usage_response.status_code == 200
    llm_usage = llm_usage_response.json()
    assert llm_usage["llm_span_count"] == 1
    assert llm_usage["total_tokens"] == 15
    assert llm_usage["total_cost_usd"] == "0.000200"


def test_tenant_isolation_against_real_clickhouse(
    real_client: TestClient,
    real_clickhouse_client,
    active_api_key: SimpleNamespace,
    revoked_api_key: SimpleNamespace,
    db_session,
) -> None:
    """Two different projects' spans must never leak into each other's
    query results, even against a real ClickHouse table shared by both."""
    from app.security.api_keys import generate_api_key
    from test_models import make_api_key

    trace_id = "fa" * 16
    span_id = "cb" * 8
    start_time, end_time = _recent_span_times()
    payload = valid_traces_payload(
        spans=[
            valid_span(
                trace_id=trace_id,
                span_id=span_id,
                name="project-a-span",
                start_time=start_time,
                end_time=end_time,
            )
        ]
    )
    headers_a = {"Authorization": f"Bearer {active_api_key.raw_key}"}

    ingest_response = _post_until_visible(
        real_client,
        real_clickhouse_client,
        payload=payload,
        headers=headers_a,
        project_id=active_api_key.project.id,
        trace_id=trace_id,
        span_id=span_id,
    )
    assert ingest_response.status_code == 200

    # revoked_api_key's raw key is (deliberately) revoked, so mint a fresh
    # active key for its same project (via the shared test db_session, the
    # same session `real_client`'s get_db override already uses) to query
    # as "project B".
    raw_key_b, key_prefix_b, key_hash_b = generate_api_key()
    make_api_key(db_session, revoked_api_key.project, key_prefix=key_prefix_b, key_hash=key_hash_b)
    db_session.commit()

    headers_b = {"Authorization": f"Bearer {raw_key_b}"}

    detail_as_b = real_client.get(f"/v1/traces/{trace_id}", headers=headers_b)
    assert detail_as_b.status_code == 404

    list_as_b = real_client.get("/v1/traces", headers=headers_b)
    assert list_as_b.status_code == 200
    assert all(t["trace_id"] != trace_id for t in list_as_b.json()["traces"])
