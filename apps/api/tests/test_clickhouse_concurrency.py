"""Regression coverage for the ClickHouse concurrent-session bug: a
thread-cached `clickhouse_connect` `Client` could be handed to two
concurrently-executing FastAPI requests at once, because a sync
`Depends()` callable and the sync route handler that uses its result are
each dispatched to Starlette's thread pool independently -- nothing
guarantees they land on the same OS thread. See
`app/clickhouse/client.py`'s module docstring for the full mechanism.

Two layers of coverage, matching the two ways this could regress:

- `test_get_clickhouse_client_never_returns_a_shared_instance_under_concurrent_use`
  unit-tests the client factory itself (mocked transport, no ClickHouse
  needed) for the actual invariant that matters now that clients carry no
  session (`autogenerate_session_id=False`): every call returns its own,
  independent object, never one shared with a concurrently-running caller.
- `test_concurrent_analytics_and_traces_requests_against_real_clickhouse_all_succeed`
  exercises the real, *unmocked* dependency chain end-to-end (real
  `get_clickhouse_client()`, real repositories, real ClickHouse) with
  genuinely concurrent HTTP requests -- the only way to catch the FastAPI
  threadpool race itself, since a mocked repository bypasses it entirely.
  Skipped automatically if ClickHouse isn't reachable, matching this
  suite's other `*_clickhouse_integration.py` tests.
"""

from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import conftest
from app.clickhouse.client import get_clickhouse_client
from app.config import settings

# Independent of the shared `db_session` fixture's single `Session` (not
# safe for concurrent use from multiple threads at once) -- same rationale
# and pattern as test_provisioning.py's `_ConcurrentTestSessionLocal`.
_ConcurrentTestSessionLocal: sessionmaker[Session] = sessionmaker(
    bind=conftest._engine, autoflush=False, expire_on_commit=False
)


def test_get_clickhouse_client_never_returns_a_shared_instance_under_concurrent_use() -> None:
    """The invariant the fix actually relies on: since clients no longer
    carry a session at all (`autogenerate_session_id=False`), asserting a
    unique `session_id` would test nothing. What has to hold instead is
    that `get_clickhouse_client()` never lets two concurrently-executing
    callers observe the *same* client object -- the fresh-client-per-call
    design makes that true by construction (no cache to leak an instance
    out of its intended caller), but this test verifies it directly,
    end-to-end through real thread scheduling, rather than by reading the
    source.
    """
    with patch("app.clickhouse.client.clickhouse_connect.get_client") as mock_get_client:
        mock_get_client.side_effect = lambda **_: object()

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(get_clickhouse_client) for _ in range(50)]
            # Every returned object is kept alive here (not just its `id()`)
            # until all 50 calls have completed: CPython reuses a freed
            # object's memory address for the next allocation, so two
            # *non-overlapping* lifetimes can share an `id()` even though
            # they were never the same object -- collecting ids while the
            # objects themselves are still discarded would make that
            # coincidence indistinguishable from the real bug.
            clients = [future.result() for future in as_completed(futures)]

    ids = [id(client) for client in clients]
    assert len(ids) == len(set(ids)), "two concurrent callers received the same client instance"
    assert mock_get_client.call_count == 50
    for _, kwargs in mock_get_client.call_args_list:
        assert kwargs["autogenerate_session_id"] is False


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
def real_client_unmocked_clickhouse(
    real_clickhouse_client,
) -> Generator[TestClient, None, None]:
    """Unlike `conftest.client` (fakes) and
    `test_query_clickhouse_integration.real_client` (a real ClickHouse
    client, but pre-bound into the repository dependencies by the test
    fixture itself -- which would only prove one shared client fails, not
    that the app's own dependency wiring is safe), this fixture overrides
    *only* `get_db`. Every ClickHouse repository dependency
    (`get_spans_repository`, `get_traces_query_repository`,
    `get_analytics_repository`, `get_evaluations_query_repository`) is left
    exactly as production wires it, so concurrent requests through this
    client exercise the real `get_..._repository()` -> `get_clickhouse_client()`
    path -- the actual code the FastAPI threadpool race lives in.

    `get_db` still needs overriding to point at the test database, but
    (unlike the single shared `db_session` other fixtures use) with a fresh
    `Session` per call -- SQLAlchemy sessions are no safer for concurrent
    use than the ClickHouse client this test exists to check, and reusing
    one here would add an unrelated source of failure to this test.
    """
    from app.db.session import get_db
    from app.main import app

    def override_get_db() -> Generator[Session, None, None]:
        session = _ConcurrentTestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_concurrent_analytics_and_traces_requests_against_real_clickhouse_all_succeed(
    real_client_unmocked_clickhouse: TestClient,
    active_api_key: SimpleNamespace,
) -> None:
    """Reproduces the real-world failure directly: 10 bursts of 8 genuinely
    concurrent identical GET /v1/analytics/spans requests (the exact shape
    that produced 2/80 HTTP 500s with "Attempt to execute concurrent
    queries within the same session" against the local production stack),
    then one burst against GET /v1/traces -- the investigation found the
    race isn't limited to analytics. Asserts zero non-200 responses.
    """
    headers = {"Authorization": f"Bearer {active_api_key.raw_key}"}

    def get_analytics(_: int):
        return real_client_unmocked_clickhouse.get(
            "/v1/analytics/spans",
            params={
                "start_time_from": "2026-09-17T00:00:00Z",
                "start_time_to": "2026-09-18T00:00:00Z",
                "bucket": "hour",
            },
            headers=headers,
        )

    for burst in range(10):
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(get_analytics, i) for i in range(8)]
            responses = [future.result() for future in as_completed(futures)]

        statuses = [response.status_code for response in responses]
        assert statuses == [200] * 8, f"burst {burst}: unexpected statuses {statuses}"

    def get_traces(_: int):
        return real_client_unmocked_clickhouse.get("/v1/traces", headers=headers)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(get_traces, i) for i in range(8)]
        trace_responses = [future.result() for future in as_completed(futures)]

    trace_statuses = [response.status_code for response in trace_responses]
    assert trace_statuses == [200] * 8, f"traces burst: unexpected statuses {trace_statuses}"
