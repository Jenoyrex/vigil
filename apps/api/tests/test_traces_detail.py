"""Route-level tests for GET /v1/traces/{trace_id}. Uses
`fake_traces_query_repository` (see tests/conftest.py) -- no real
ClickHouse involved.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"

_SUMMARY = {
    "total_span_count": 2,
    "error_span_count": 0,
    "root_span_count": 1,
    "trace_start_time": datetime(2026, 8, 31, 12, 0, 0),
    "trace_end_time": datetime(2026, 8, 31, 12, 0, 1, 250000),
}


def _span_row(**overrides):
    row = {
        "span_id": "00f067aa0ba902b7",
        "parent_span_id": None,
        "name": "checkout.process_order",
        "span_type": "agent",
        "resource": "checkout-service",
        "start_time": datetime(2026, 8, 31, 12, 0, 0),
        "end_time": datetime(2026, 8, 31, 12, 0, 1, 250000),
        "duration_ms": 1250,
        "status": "ok",
        "status_message": None,
        "input": None,
        "input_size_bytes": 0,
        "input_truncated": False,
        "output": None,
        "output_size_bytes": 0,
        "output_truncated": False,
        "attributes": {},
        "attributes_truncated": False,
        "events.time": [],
        "events.name": [],
        "events.attributes": [],
        "events_truncated": False,
        "llm_provider": None,
        "llm_model": None,
        "llm_input_tokens": None,
        "llm_output_tokens": None,
        "llm_total_tokens": None,
        "llm_cost_usd": None,
        "environment": "production",
        "release": None,
    }
    row.update(overrides)
    return row


def _auth_headers(active_api_key: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {active_api_key.raw_key}"}


def test_missing_auth_is_401(client: TestClient) -> None:
    response = client.get(f"/v1/traces/{TRACE_ID}")
    assert response.status_code == 401


def test_malformed_trace_id_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get("/v1/traces/not-a-valid-trace-id", headers=_auth_headers(active_api_key))
    assert response.status_code == 422


def test_short_trace_id_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get("/v1/traces/abc123", headers=_auth_headers(active_api_key))
    assert response.status_code == 422


def test_not_found_returns_404(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.summarize_trace_result = None
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    assert response.status_code == 404


def test_project_id_comes_from_auth(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.summarize_trace_result = _SUMMARY
    fake_traces_query_repository.get_trace_spans_result = [_span_row()]
    client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    [summary_call] = fake_traces_query_repository.summarize_trace_calls
    [spans_call] = fake_traces_query_repository.get_trace_spans_calls
    assert summary_call["project_id"] == active_api_key.project.id
    assert spans_call["project_id"] == active_api_key.project.id


def test_response_shape_and_status_derivation(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.summarize_trace_result = _SUMMARY
    fake_traces_query_repository.get_trace_spans_result = [_span_row(), _span_row(span_id="b" * 16)]
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == TRACE_ID
    assert body["status"] == "ok"
    assert body["duration_ms"] == 1250
    assert body["span_count"] == 2
    assert body["total_span_count"] == 2
    assert body["truncated"] is False
    assert len(body["spans"]) == 2


def test_spans_ordered_start_time_asc_requested_from_repository(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    """The ordering itself is a repository-level concern (see
    test_query_repository.py); this just confirms the route wires through
    without re-sorting/reversing what the repository returned."""
    fake_traces_query_repository.summarize_trace_result = _SUMMARY
    first = _span_row(span_id="1" * 16, name="first")
    second = _span_row(span_id="2" * 16, name="second")
    fake_traces_query_repository.get_trace_spans_result = [first, second]
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    names = [s["name"] for s in response.json()["spans"]]
    assert names == ["first", "second"]


def test_truncation_flag_and_total_span_count(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    """Repository is asked for max_spans+1 rows; if it returns more than
    max_spans, `truncated` is set and only max_spans are returned, while
    total_span_count comes from the separate, untruncated summary."""
    from app.config import settings

    max_spans = settings.max_spans_per_trace_response
    fake_traces_query_repository.summarize_trace_result = {
        **_SUMMARY, "total_span_count": max_spans + 500,
    }
    fake_traces_query_repository.get_trace_spans_result = [
        _span_row(span_id=f"{i:016x}") for i in range(max_spans + 1)
    ]
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    body = response.json()
    assert body["truncated"] is True
    assert body["span_count"] == max_spans
    assert len(body["spans"]) == max_spans
    assert body["total_span_count"] == max_spans + 500


def test_error_status_when_any_span_errored(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.summarize_trace_result = {**_SUMMARY, "error_span_count": 1}
    fake_traces_query_repository.get_trace_spans_result = [_span_row(status="error")]
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    assert response.json()["status"] == "error"


def test_unknown_status_when_no_root_span(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.summarize_trace_result = {**_SUMMARY, "root_span_count": 0}
    fake_traces_query_repository.get_trace_spans_result = [
        _span_row(parent_span_id="a" * 16)
    ]
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    assert response.json()["status"] == "unknown"


def test_llm_cost_serialized_as_string(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    from decimal import Decimal

    fake_traces_query_repository.summarize_trace_result = _SUMMARY
    fake_traces_query_repository.get_trace_spans_result = [
        _span_row(llm_provider="openai", llm_cost_usd=Decimal("0.000123"))
    ]
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    span = response.json()["spans"][0]
    assert span["llm_cost_usd"] == "0.000123"
    assert isinstance(span["llm_cost_usd"], str)


def test_events_are_included(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.summarize_trace_result = _SUMMARY
    fake_traces_query_repository.get_trace_spans_result = [
        _span_row(
            **{
                "events.time": [datetime(2026, 8, 31, 12, 0, 0, 500000)],
                "events.name": ["first_token"],
                "events.attributes": [{}],
            }
        )
    ]
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    events = response.json()["spans"][0]["events"]
    assert events == [
        {"time": "2026-08-31T12:00:00.500000Z", "name": "first_token", "attributes": {}}
    ]


def test_start_date_hint_is_forwarded(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.summarize_trace_result = _SUMMARY
    fake_traces_query_repository.get_trace_spans_result = [_span_row()]
    client.get(
        f"/v1/traces/{TRACE_ID}",
        params={"start_date": "2026-08-31"},
        headers=_auth_headers(active_api_key),
    )
    [summary_call] = fake_traces_query_repository.summarize_trace_calls
    assert str(summary_call["start_date"]) == "2026-08-31"


def test_clickhouse_unavailable_is_503(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    from app.clickhouse.repository import ClickHouseUnavailableError

    fake_traces_query_repository.fail_with = ClickHouseUnavailableError("down")
    response = client.get(f"/v1/traces/{TRACE_ID}", headers=_auth_headers(active_api_key))
    assert response.status_code == 503
