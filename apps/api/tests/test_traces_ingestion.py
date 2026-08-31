"""Transformation, payload limits, and ClickHouse-failure handling for
POST /v1/traces. Uses the fake repository (see tests/conftest.py) --
tests/test_traces_clickhouse_integration.py covers the real ClickHouse path.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.clickhouse.repository import ClickHouseInsertError, ClickHouseUnavailableError
from app.config import settings
from helpers import valid_span, valid_traces_payload


def _post(client: TestClient, active_api_key: SimpleNamespace, **payload_overrides):
    payload = valid_traces_payload(**payload_overrides)
    return client.post(
        "/v1/traces",
        json=payload,
        headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
    )


# --- transformation -----------------------------------------------------


def test_resource_metadata_is_mapped(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    response = _post(
        client,
        active_api_key,
        resource={
            "service.name": "svc-a",
            "sdk.name": "vigil-python",
            "sdk.version": "1.2.3",
            "custom.field": "hello",
        },
        spans=[valid_span(attributes={"foo": "bar"})],
    )

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["resource"] == "svc-a"
    assert row["attributes"]["resource.sdk.name"] == "vigil-python"
    assert row["attributes"]["resource.sdk.version"] == "1.2.3"
    assert row["attributes"]["resource.custom.field"] == "hello"
    assert row["attributes"]["foo"] == "bar"


def test_llm_fields_are_mapped(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    response = _post(
        client,
        active_api_key,
        spans=[
            valid_span(
                llm_provider="openai",
                llm_model="gpt-4o-mini",
                llm_input_tokens=10,
                llm_output_tokens=5,
                llm_total_tokens=15,
                llm_cost_usd=0.002,
            )
        ],
    )

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["llm_provider"] == "openai"
    assert row["llm_model"] == "gpt-4o-mini"
    assert row["llm_input_tokens"] == 10
    assert row["llm_output_tokens"] == 5
    assert row["llm_total_tokens"] == 15
    assert row["llm_cost_usd"] == Decimal("0.002")


def test_llm_fields_absent_when_not_supplied(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    response = _post(client, active_api_key, spans=[valid_span()])

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["llm_provider"] is None
    assert row["llm_cost_usd"] is None


def test_attributes_and_events_are_mapped(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    response = _post(
        client,
        active_api_key,
        spans=[
            valid_span(
                attributes={"a": "b", "n": 5, "flag": True},
                events=[
                    {
                        "time": "2026-01-01T00:00:00.5Z",
                        "name": "first_token",
                        "attributes": {"x": "y"},
                    }
                ],
            )
        ],
    )

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["attributes"]["a"] == "b"
    assert row["attributes"]["n"] == "5"
    assert row["attributes"]["flag"] == "true"
    assert row["events.name"] == ["first_token"]
    assert row["events.attributes"] == [{"x": "y"}]


def test_duration_is_not_client_authoritative(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    """duration_ms is MATERIALIZED in ClickHouse; a client-sent value must be
    dropped, not passed through to the repository."""
    response = _post(client, active_api_key, spans=[valid_span(duration_ms=999999)])

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert "duration_ms" not in row


def test_client_supplied_project_id_is_ignored(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    other_project_id = "00000000-0000-4000-8000-000000000099"
    response = _post(client, active_api_key, spans=[valid_span(project_id=other_project_id)])

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["project_id"] == active_api_key.project.id
    assert str(row["project_id"]) != other_project_id


# --- payload limits -------------------------------------------------------


def test_input_is_truncated_over_limit(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    oversized = "x" * (settings.max_input_bytes + 100)
    response = _post(client, active_api_key, spans=[valid_span(input=oversized)])

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["input_truncated"] is True
    assert row["input_size_bytes"] == len(oversized.encode("utf-8"))
    assert len(row["input"].encode("utf-8")) <= settings.max_input_bytes


def test_output_is_truncated_over_limit(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    oversized = "y" * (settings.max_output_bytes + 100)
    response = _post(client, active_api_key, spans=[valid_span(output=oversized)])

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["output_truncated"] is True
    assert row["output_size_bytes"] == len(oversized.encode("utf-8"))
    assert len(row["output"].encode("utf-8")) <= settings.max_output_bytes


def test_input_not_truncated_when_under_limit(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    small = "hello world"
    response = _post(client, active_api_key, spans=[valid_span(input=small)])

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["input_truncated"] is False
    assert row["input"] == small
    assert row["input_size_bytes"] == len(small.encode("utf-8"))


def test_truncation_counts_utf8_bytes_not_characters(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    """Each rocket emoji is 4 UTF-8 bytes; char count alone would look "small"."""
    char_count = 20_000
    oversized = "\U0001f680" * char_count  # 80,000 bytes, well over the 64 KiB cap
    assert char_count < settings.max_input_bytes < len(oversized.encode("utf-8"))

    response = _post(client, active_api_key, spans=[valid_span(input=oversized)])

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["input_truncated"] is True
    assert row["input_size_bytes"] == len(oversized.encode("utf-8"))
    stored_bytes = row["input"].encode("utf-8")
    assert len(stored_bytes) <= settings.max_input_bytes
    # truncation must land on a valid UTF-8 boundary
    stored_bytes.decode("utf-8")


def test_oversized_total_span_payload_truncates_attributes(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    big_attributes = {f"key-{i}": "v" * 1000 for i in range(500)}  # ~500 KiB, over budget
    response = _post(client, active_api_key, spans=[valid_span(attributes=big_attributes)])

    assert response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["attributes_truncated"] is True
    assert len(row["attributes"]) < len(big_attributes)


def test_oversized_request_body_is_413(client: TestClient) -> None:
    huge_input = "x" * (settings.max_request_body_bytes + 1024)
    payload = valid_traces_payload(spans=[valid_span(input=huge_input)])

    # Deliberately no Authorization header: the body-size middleware runs
    # before any dependency (including auth) is resolved.
    response = client.post("/v1/traces", json=payload)

    assert response.status_code == 413


# --- ClickHouse failure handling -----------------------------------------


def test_clickhouse_unavailable_is_503(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    fake_repository.fail_with = ClickHouseUnavailableError("connection refused")
    response = _post(client, active_api_key, spans=[valid_span()])

    assert response.status_code == 503
    assert "credentials" not in response.text.lower()


def test_clickhouse_insert_error_is_500(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    fake_repository.fail_with = ClickHouseInsertError("type mismatch")
    response = _post(client, active_api_key, spans=[valid_span()])

    assert response.status_code == 500
    assert "type mismatch" not in response.text


def test_repository_receives_one_batch_for_multiple_spans(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    spans = [valid_span(trace_id=f"{i:032x}", span_id=f"{i:016x}") for i in range(5)]
    response = _post(client, active_api_key, spans=spans)

    assert response.status_code == 200
    assert response.json()["accepted"] == 5
    assert len(fake_repository.batches) == 1
    assert len(fake_repository.batches[0]) == 5
