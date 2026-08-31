"""Request validation for POST /v1/traces. See app/schemas/traces.py."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from helpers import valid_span, valid_traces_payload


def _post(client: TestClient, active_api_key: SimpleNamespace, **payload_overrides):
    payload = valid_traces_payload(**payload_overrides)
    return client.post(
        "/v1/traces",
        json=payload,
        headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
    )


def test_invalid_trace_id_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = _post(client, active_api_key, spans=[valid_span(trace_id="not-hex")])
    assert response.status_code == 422


def test_invalid_span_id_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = _post(client, active_api_key, spans=[valid_span(span_id="short")])
    assert response.status_code == 422


def test_invalid_parent_span_id_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = _post(client, active_api_key, spans=[valid_span(parent_span_id="not-16-hex-chars")])
    assert response.status_code == 422


def test_empty_span_name_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = _post(client, active_api_key, spans=[valid_span(name="")])
    assert response.status_code == 422


def test_whitespace_only_span_name_is_422(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    response = _post(client, active_api_key, spans=[valid_span(name="   ")])
    assert response.status_code == 422


def test_invalid_status_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = _post(client, active_api_key, spans=[valid_span(status="not-a-status")])
    assert response.status_code == 422


def test_end_before_start_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = _post(
        client,
        active_api_key,
        spans=[
            valid_span(start_time="2026-01-01T00:00:10Z", end_time="2026-01-01T00:00:00Z")
        ],
    )
    assert response.status_code == 422


def test_empty_spans_array_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = _post(client, active_api_key, spans=[])
    assert response.status_code == 422


def test_too_many_spans_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    from app.config import settings

    too_many = [
        valid_span(trace_id=f"{i:032x}", span_id=f"{i:016x}")
        for i in range(settings.max_spans_per_request + 1)
    ]
    response = _post(client, active_api_key, spans=too_many)
    assert response.status_code == 422


def test_unknown_span_type_is_accepted(client: TestClient, active_api_key: SimpleNamespace) -> None:
    """span_type is an open string -- unrecognized values must not be rejected."""
    response = _post(
        client, active_api_key, spans=[valid_span(span_type="some-brand-new-span-kind")]
    )
    assert response.status_code == 200
