"""Real-PostgreSQL proof that `worker.postgres.client.get_connection()`'s
timeout configuration (Phase 4D, F5) actually bounds both connection
establishment and server-side statement execution -- not merely that some
keyword argument was passed. See that module's own docstring for the exact
mechanism (libpq `connect_timeout` + PostgreSQL's `statement_timeout` GUC).

Skipped automatically if PostgreSQL is unreachable, mirroring every other
real-store integration test in this package (test_dispatcher_integration.py
etc.) -- except `test_connect_timeout_bounds_an_unroutable_host`, which
deliberately tests unreachability itself and must never be skipped for it.
"""

from __future__ import annotations

import os
import time

import psycopg
import pytest

import worker.config
from worker.postgres.client import get_connection

PG_TEST_DATABASE_URL = os.environ.get(
    "VIGIL_WORKER_TEST_DATABASE_URL",
    "postgresql://vigil:vigil@localhost:5434/vigil_test",
)
if not PG_TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("test"):
    raise RuntimeError(
        "VIGIL_WORKER_TEST_DATABASE_URL does not point at a database named "
        "'*test' -- refusing to run destructive tests against it."
    )


@pytest.fixture
def real_postgres(monkeypatch: pytest.MonkeyPatch):
    """Points `worker.config.settings.database_url` at the real test
    database for the duration of one test, skipping if it's unreachable --
    the identical convention every other real-store test in this package
    already uses."""
    try:
        probe = psycopg.connect(PG_TEST_DATABASE_URL, connect_timeout=2)
        probe.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"PostgreSQL not reachable at {PG_TEST_DATABASE_URL} ({exc}).")

    monkeypatch.setattr(worker.config.settings, "database_url", PG_TEST_DATABASE_URL)


def test_statement_timeout_is_actually_set_on_the_server(
    real_postgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queries the server's own session state (`SHOW statement_timeout`)
    rather than inspecting client-side call arguments -- this is what
    proves the GUC was genuinely applied server-side, not just constructed
    correctly on the client."""
    monkeypatch.setattr(worker.config.settings, "database_timeout_seconds", 7.0)

    connection = get_connection()
    try:
        result = connection.execute("SHOW statement_timeout").fetchone()
    finally:
        connection.close()

    assert result is not None
    assert result[0] == "7s"


def test_statement_timeout_actually_cancels_a_slow_query(
    real_postgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empirical proof the task requires: a query that would otherwise
    take 5 real seconds (`pg_sleep(5)`) is cancelled by the server well
    before that, with a `database_timeout_seconds` of 1 -- not merely
    "eventually raises after the full sleep completes", which would prove
    nothing about the timeout actually working.
    """
    monkeypatch.setattr(worker.config.settings, "database_timeout_seconds", 1.0)

    connection = get_connection()
    try:
        started = time.monotonic()
        with pytest.raises(psycopg.errors.QueryCanceled):
            connection.execute("SELECT pg_sleep(5)")
        elapsed = time.monotonic() - started
    finally:
        connection.close()

    # Generous upper bound (not exactly 1s) to stay robust against CI
    # scheduling jitter, while still being far below the 5s the query
    # would take if the timeout did nothing at all.
    assert elapsed < 3.0


def test_connection_is_reusable_after_a_cancelled_statement(
    real_postgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled statement under autocommit must not leave the connection
    in a broken/aborted state -- the exact concern
    `worker.dispatcher.Dispatcher._run_one` depends on implicitly: the same
    `jobs_repository`/connection execute_job was given is reused, unchanged,
    for failure handling after any exception (see that module's docstring).
    """
    monkeypatch.setattr(worker.config.settings, "database_timeout_seconds", 1.0)

    connection = get_connection()
    try:
        with pytest.raises(psycopg.errors.QueryCanceled):
            connection.execute("SELECT pg_sleep(5)")

        # The same connection must still work for a completely ordinary
        # statement right after the cancellation, with no explicit
        # ROLLBACK/reset -- autocommit means each statement is its own
        # transaction (see worker/postgres/client.py's own docstring).
        result = connection.execute("SELECT 1").fetchone()
        assert result == (1,)
    finally:
        connection.close()


def test_connect_timeout_bounds_an_unroutable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Does not use the `real_postgres` fixture and must never be
    skipped -- this test's entire point is that PostgreSQL is NOT
    reachable, via a deliberately unroutable address (192.0.2.1, RFC 5737
    TEST-NET-1, reserved for exactly this purpose: guaranteed to never
    route to a real host anywhere).

    Without `connect_timeout`, connecting to an unroutable (not merely
    refusing) address can hang far longer than any reasonable bound --
    depending on OS-level TCP retransmission behavior, sometimes minutes.
    With `database_timeout_seconds=2`, this must fail fast instead.
    """
    monkeypatch.setattr(worker.config.settings, "database_timeout_seconds", 2.0)
    monkeypatch.setattr(
        worker.config.settings,
        "database_url",
        "postgresql://vigil:vigil@192.0.2.1:5432/vigil",
    )

    started = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        get_connection()
    elapsed = time.monotonic() - started

    # Generous upper bound relative to the configured 2s connect_timeout,
    # while remaining far below what an unbounded OS-level TCP timeout
    # would take.
    assert elapsed < 10.0
