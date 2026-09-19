"""`app.middleware.RequestIdMiddleware` -- Phase 4D, F4. Full-stack tests
(via the real `app.main.app`, exactly as deployed) proving:

1. every response carries an `X-Request-Id` header;
2. two different requests get two different ids (never reused/cached);
3. a client-supplied `X-Request-Id` is never trusted -- this middleware
   always mints its own, since nothing about a log-correlation id benefits
   from being caller-controlled;
4. `POST /v1/traces`'s own `request_id` response-body field is the exact
   same value as the header (app/api/v1/traces.py now reuses the
   middleware's id via `app.logging_config.get_request_id()` instead of
   minting a second one);
5. the id is actually available for structured-log correlation during
   request handling -- not just echoed back on the response.
"""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.clickhouse.repository import ClickHouseUnavailableError
from app.logging_config import JsonFormatter, _request_id_var
from helpers import valid_traces_payload

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE
)


def test_response_has_an_x_request_id_header(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert _UUID4_RE.match(response.headers["x-request-id"])


def test_each_request_gets_a_distinct_request_id(client: TestClient) -> None:
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]

    assert first != second


def test_client_supplied_request_id_header_is_ignored(client: TestClient) -> None:
    attacker_supplied = "not-a-real-id; DROP TABLE evaluation_jobs;"

    response = client.get("/health", headers={"X-Request-Id": attacker_supplied})

    assert response.headers["x-request-id"] != attacker_supplied
    assert _UUID4_RE.match(response.headers["x-request-id"])


def test_traces_ingest_response_request_id_matches_header(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    response = client.post(
        "/v1/traces",
        json=valid_traces_payload(),
        headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
    )

    assert response.status_code == 200
    assert response.json()["request_id"] == response.headers["x-request-id"]


class _CapturingHandler(logging.Handler):
    """Runs every captured record through the real `JsonFormatter` --
    unlike pytest's own `caplog` handler (a separate handler with no
    formatter of its own), this proves the actual production JSON output
    contains the field, not just a raw LogRecord attribute.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter(service="api"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_request_id_is_bound_during_log_calls_made_while_handling_the_request(
    client: TestClient, active_api_key: SimpleNamespace, fake_repository
) -> None:
    """Proves the id is genuinely usable for correlation, not merely
    returned to the caller: a log call made deep in the ClickHouse-failure
    path (app/api/v1/traces.py), with no request_id argument threaded
    through explicitly, still lands in the emitted JSON with the same id
    this test's own request got back."""
    fake_repository.fail_with = ClickHouseUnavailableError("boom")

    capture = _CapturingHandler()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        response = client.post(
            "/v1/traces",
            json=valid_traces_payload(),
            headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
        )
    finally:
        root.removeHandler(capture)

    assert response.status_code == 503
    returned_request_id = response.headers["x-request-id"]

    # The contextvar must be reset once the request finishes -- otherwise a
    # later, unrelated request on a reused thread could inherit a stale id.
    assert _request_id_var.get() is None

    records = [json.loads(line) for line in capture.lines]
    matching = [record for record in records if record.get("request_id") == returned_request_id]
    assert matching, f"expected a log record carrying request_id={returned_request_id!r}"
    # NEVER the raw exception's ClickHouse-driver text leaking span payload
    # content -- only the identifiers/error-type this call site's own
    # extra={...} passes. See app/logging_config.py's security note.
    assert all("input" not in record and "output" not in record for record in records)
