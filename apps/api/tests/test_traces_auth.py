"""Authentication behavior for POST /v1/traces. See app/api/deps.py."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from helpers import valid_span, valid_traces_payload


def test_missing_authorization_header_is_401(client: TestClient) -> None:
    response = client.post("/v1/traces", json=valid_traces_payload())

    assert response.status_code == 401


def test_malformed_authorization_header_is_401(client: TestClient) -> None:
    response = client.post(
        "/v1/traces",
        json=valid_traces_payload(),
        headers={"Authorization": "NotBearer something"},
    )

    assert response.status_code == 401


def test_bearer_with_empty_token_is_401(client: TestClient) -> None:
    response = client.post(
        "/v1/traces", json=valid_traces_payload(), headers={"Authorization": "Bearer "}
    )

    assert response.status_code == 401


def test_unknown_api_key_is_401(client: TestClient) -> None:
    response = client.post(
        "/v1/traces",
        json=valid_traces_payload(),
        headers={"Authorization": "Bearer vgl_deadbeef.not-a-real-secret"},
    )

    assert response.status_code == 401


def test_revoked_api_key_is_401(client: TestClient, revoked_api_key: SimpleNamespace) -> None:
    response = client.post(
        "/v1/traces",
        json=valid_traces_payload(),
        headers={"Authorization": f"Bearer {revoked_api_key.raw_key}"},
    )

    assert response.status_code == 401


def test_valid_api_key_resolves_correct_project(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    response = client.post(
        "/v1/traces",
        json=valid_traces_payload(spans=[valid_span(trace_id="a" * 32, span_id="b" * 16)]),
        headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert len(fake_repository.batches) == 1
    [row] = fake_repository.batches[0]
    assert row["project_id"] == active_api_key.project.id
