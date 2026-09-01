"""Route-level tests for GET /v1/traces. Uses `fake_traces_query_repository`
(see tests/conftest.py) -- no real ClickHouse involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def _row(**overrides):
    row = {
        "trace_id": TRACE_ID,
        "trace_start_time": datetime(2026, 8, 31, 12, 0, 0),
        "trace_end_time": datetime(2026, 8, 31, 12, 0, 1, 250000),
        "span_count": 3,
        "error_span_count": 0,
        "root_span_count": 1,
        "root_span_name": "checkout.process_order",
        "trace_environment": "production",
        "trace_resource": "checkout-service",
    }
    row.update(overrides)
    return row


def _auth_headers(active_api_key: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {active_api_key.raw_key}"}


# -- authentication -----------------------------------------------------


def test_missing_auth_is_401(client: TestClient) -> None:
    response = client.get("/v1/traces")
    assert response.status_code == 401


def test_unknown_api_key_is_401(client: TestClient) -> None:
    response = client.get(
        "/v1/traces", headers={"Authorization": "Bearer vgl_deadbeef.not-a-real-secret"}
    )
    assert response.status_code == 401


# -- project isolation -----------------------------------------------------


def test_project_id_comes_from_auth_not_client(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    """Even if a project_id-shaped query param is sent, the repository is
    called with auth.project_id -- there is no project_id parameter on this
    route at all, so FastAPI never binds a client-supplied value to one."""
    fake_traces_query_repository.list_traces_result = [_row()]
    response = client.get(
        "/v1/traces",
        params={"project_id": "00000000-0000-0000-0000-000000000000"},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    [call] = fake_traces_query_repository.list_traces_calls
    assert call["project_id"] == active_api_key.project.id


# -- time window ------------------------------------------------------------


def test_omitted_window_defaults_to_last_24h(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = []
    response = client.get("/v1/traces", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    [call] = fake_traces_query_repository.list_traces_calls
    delta = call["start_time_to"] - call["start_time_from"]
    assert abs(delta.total_seconds() - 24 * 3600) < 5


def test_window_wider_than_max_is_422(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    response = client.get(
        "/v1/traces",
        params={
            "start_time_from": "2026-08-01T00:00:00Z",
            "start_time_to": "2026-08-31T00:00:00Z",
        },
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_start_after_end_is_422(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    response = client.get(
        "/v1/traces",
        params={
            "start_time_from": "2026-08-31T00:00:00Z",
            "start_time_to": "2026-08-30T00:00:00Z",
        },
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_naive_timestamp_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get(
        "/v1/traces",
        params={"start_time_from": "2026-08-31T00:00:00"},  # no offset
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


# -- limit --------------------------------------------------------------


def test_limit_defaults_to_20(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = []
    client.get("/v1/traces", headers=_auth_headers(active_api_key))
    [call] = fake_traces_query_repository.list_traces_calls
    assert call["limit"] == 21  # service fetches limit+1 to detect a next page


def test_limit_over_100_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get(
        "/v1/traces", params={"limit": 101}, headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 422


def test_limit_under_1_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get("/v1/traces", params={"limit": 0}, headers=_auth_headers(active_api_key))
    assert response.status_code == 422


# -- cursor pagination --------------------------------------------------


def test_malformed_cursor_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get(
        "/v1/traces", params={"cursor": "not-valid"}, headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 422


def test_next_cursor_present_when_more_results_exist(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = [
        _row(trace_id=f"{i:032x}") for i in range(21)  # limit(20) + 1 lookahead row
    ]
    response = client.get(
        "/v1/traces", params={"limit": 20}, headers=_auth_headers(active_api_key)
    )
    body = response.json()
    assert len(body["traces"]) == 20
    assert body["next_cursor"] is not None


def test_next_cursor_absent_when_no_more_results(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = [_row()]
    response = client.get(
        "/v1/traces", params={"limit": 20}, headers=_auth_headers(active_api_key)
    )
    body = response.json()
    assert len(body["traces"]) == 1
    assert body["next_cursor"] is None


def test_cursor_is_decoded_and_passed_to_repository(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    from app.services.query import encode_trace_cursor

    fake_traces_query_repository.list_traces_result = []
    cursor_time = datetime(2026, 8, 30, 0, 0, 0, tzinfo=UTC)
    cursor = encode_trace_cursor(cursor_time, TRACE_ID)
    client.get("/v1/traces", params={"cursor": cursor}, headers=_auth_headers(active_api_key))
    [call] = fake_traces_query_repository.list_traces_calls
    assert call["cursor"] == (cursor_time, TRACE_ID)


# -- trace status derivation & response shape --------------------------


def test_status_error_when_error_spans_present(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = [_row(error_span_count=1, root_span_count=1)]
    response = client.get("/v1/traces", headers=_auth_headers(active_api_key))
    assert response.json()["traces"][0]["status"] == "error"


def test_status_ok_when_root_span_present_and_no_errors(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = [_row(error_span_count=0, root_span_count=1)]
    response = client.get("/v1/traces", headers=_auth_headers(active_api_key))
    assert response.json()["traces"][0]["status"] == "ok"


def test_status_unknown_when_no_root_span_and_no_errors(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = [_row(error_span_count=0, root_span_count=0)]
    response = client.get("/v1/traces", headers=_auth_headers(active_api_key))
    assert response.json()["traces"][0]["status"] == "unknown"


def test_response_includes_all_documented_fields(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = [_row()]
    response = client.get("/v1/traces", headers=_auth_headers(active_api_key))
    [trace] = response.json()["traces"]
    assert set(trace.keys()) == {
        "trace_id", "start_time", "end_time", "duration_ms", "status",
        "span_count", "error_span_count", "root_span_name", "environment", "resource",
    }
    assert trace["duration_ms"] == 1250


def test_has_error_filter_is_forwarded(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.list_traces_result = []
    client.get("/v1/traces", params={"has_error": "true"}, headers=_auth_headers(active_api_key))
    [call] = fake_traces_query_repository.list_traces_calls
    assert call["has_error"] is True


# -- ClickHouse errors -> 503 --------------------------------------------


def test_clickhouse_unavailable_is_503(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    from app.clickhouse.repository import ClickHouseUnavailableError

    fake_traces_query_repository.fail_with = ClickHouseUnavailableError("down")
    response = client.get("/v1/traces", headers=_auth_headers(active_api_key))
    assert response.status_code == 503


def test_clickhouse_query_error_is_500(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    from app.clickhouse.query_common import ClickHouseQueryError

    fake_traces_query_repository.fail_with = ClickHouseQueryError("bad query")
    response = client.get("/v1/traces", headers=_auth_headers(active_api_key))
    assert response.status_code == 500
