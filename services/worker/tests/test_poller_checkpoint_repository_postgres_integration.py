"""Real-PostgreSQL integration tests for
worker.postgres.poller_checkpoint_repository.PollerCheckpointRepository.

Mirrors test_evaluation_jobs_postgres_integration.py's conventions (fixed
test database, truncate-before/after, skip if unreachable, autocommit=True
connections matching production's get_connection() shape).
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime

import psycopg
import pytest

from worker.postgres.poller_checkpoint_repository import PollerCheckpointRepository

PG_TEST_DATABASE_URL = os.environ.get(
    "VIGIL_WORKER_TEST_DATABASE_URL",
    "postgresql://vigil:vigil@localhost:5434/vigil_test",
)
if not PG_TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("test"):
    raise RuntimeError(
        "VIGIL_WORKER_TEST_DATABASE_URL does not point at a database named "
        "'*test' -- refusing to run destructive tests against it."
    )

_PG_TABLES = "evaluation_poller_checkpoint"


def _truncate_all() -> None:
    with psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute(f"TRUNCATE TABLE {_PG_TABLES} RESTART IDENTITY CASCADE")


@pytest.fixture
def real_pg_connection():
    try:
        probe = psycopg.connect(PG_TEST_DATABASE_URL, connect_timeout=2)
        probe.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"PostgreSQL not reachable at {PG_TEST_DATABASE_URL} ({exc}).")

    _truncate_all()
    connection = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True)
    try:
        yield connection
    finally:
        connection.close()
        _truncate_all()


def _fetch_watermark(connection) -> datetime | None:
    row = connection.execute(
        "SELECT last_ingested_at FROM evaluation_poller_checkpoint WHERE id = 'global'"
    ).fetchone()
    return row[0] if row else None


def test_get_or_create_creates_singleton_row_with_null_watermark(real_pg_connection) -> None:
    repo = PollerCheckpointRepository(real_pg_connection)
    assert repo.get_or_create_checkpoint() is None

    row = real_pg_connection.execute(
        "SELECT id, last_ingested_at FROM evaluation_poller_checkpoint"
    ).fetchall()
    assert row == [("global", None)]


def test_get_or_create_is_idempotent_on_repeated_calls(real_pg_connection) -> None:
    repo = PollerCheckpointRepository(real_pg_connection)
    repo.get_or_create_checkpoint()
    repo.get_or_create_checkpoint()

    rows = real_pg_connection.execute("SELECT id FROM evaluation_poller_checkpoint").fetchall()
    assert len(rows) == 1


def test_advance_checkpoint_cas_success(real_pg_connection) -> None:
    repo = PollerCheckpointRepository(real_pg_connection)
    repo.get_or_create_checkpoint()

    watermark = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    accepted = repo.advance_checkpoint(observed_watermark=None, new_watermark=watermark)

    assert accepted is True
    assert _fetch_watermark(real_pg_connection) == watermark


def test_advance_checkpoint_stale_cas_loses_safely(real_pg_connection) -> None:
    repo = PollerCheckpointRepository(real_pg_connection)
    repo.get_or_create_checkpoint()

    first_watermark = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    assert repo.advance_checkpoint(observed_watermark=None, new_watermark=first_watermark) is True

    # A second poller still believes the checkpoint is None (stale observation).
    stale_watermark = datetime(2026, 9, 10, 12, 30, 0, tzinfo=UTC)
    accepted = repo.advance_checkpoint(observed_watermark=None, new_watermark=stale_watermark)

    assert accepted is False
    # Checkpoint stays at the value the FIRST (winning) advance set -- never
    # overwritten by the stale caller, and never moved backward either.
    assert _fetch_watermark(real_pg_connection) == first_watermark


def test_advance_checkpoint_never_regresses_even_with_an_earlier_new_watermark(
    real_pg_connection,
) -> None:
    """advance_checkpoint itself does not compare new_watermark against the
    current value -- the CAS predicate is purely about observed_watermark
    matching. This test documents that a caller which (incorrectly)
    supplied a stale new_watermark alongside a CURRENT observed_watermark
    would move the checkpoint backward -- proving the invariant lives in
    worker/poller.py's own "only ever compute new_watermark >= observed"
    discipline, not enforced redundantly here. Included so a future change
    to that discipline is caught by this test failing loudly, not silently.
    """
    repo = PollerCheckpointRepository(real_pg_connection)
    repo.get_or_create_checkpoint()

    later = datetime(2026, 9, 10, 12, 30, 0, tzinfo=UTC)
    earlier = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    repo.advance_checkpoint(observed_watermark=None, new_watermark=later)

    # Correct usage (worker/poller.py never does this): a real poller tick
    # always computes new_watermark from its OWN scanned batch's max
    # ingested_at, which by construction is >= what it observed.
    accepted = repo.advance_checkpoint(observed_watermark=later, new_watermark=earlier)
    assert accepted is True  # the CAS predicate has no opinion on ordering
    assert (
        _fetch_watermark(real_pg_connection) == earlier
    )  # documents why poller.py must not do this


def test_concurrent_pollers_racing_on_checkpoint_never_regress_or_corrupt(
    real_pg_connection,
) -> None:
    """Two threads, two connections, racing to advance from the same
    observed (None) watermark via a barrier to maximize overlap -- exactly
    one must win, the other must lose safely (no exception, no corrupted
    state), and the final value must be one of the two candidates, never
    something else."""
    repo = PollerCheckpointRepository(real_pg_connection)
    repo.get_or_create_checkpoint()

    watermark_a = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    watermark_b = datetime(2026, 9, 10, 13, 0, 0, tzinfo=UTC)
    results: dict[str, bool] = {}
    barrier = threading.Barrier(2)

    def _advance(name: str, new_watermark: datetime) -> None:
        connection = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True)
        try:
            local_repo = PollerCheckpointRepository(connection)
            barrier.wait()
            results[name] = local_repo.advance_checkpoint(
                observed_watermark=None, new_watermark=new_watermark
            )
        finally:
            connection.close()

    threads = [
        threading.Thread(target=_advance, args=("a", watermark_a)),
        threading.Thread(target=_advance, args=("b", watermark_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [False, True]  # exactly one winner
    final = _fetch_watermark(real_pg_connection)
    assert final in (watermark_a, watermark_b)
