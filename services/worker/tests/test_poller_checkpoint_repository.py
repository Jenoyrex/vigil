"""Unit tests for worker.postgres.poller_checkpoint_repository.PollerCheckpointRepository.

Uses `fake_postgres_connection` (tests/conftest.py) -- these tests assert
the exact SQL text/parameters passed to `.execute(...)`, in particular that
`advance_checkpoint`'s WHERE clause includes the optimistic-CAS predicate.
Real PostgreSQL concurrency/CAS behavior is proven separately in
test_poller_checkpoint_repository_postgres_integration.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from worker.postgres.poller_checkpoint_repository import PollerCheckpointRepository

WATERMARK = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def test_get_or_create_ensures_singleton_row_first(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=0)  # ensure-row INSERT
    fake_postgres_connection.queue_result(rows=[(None,)])  # SELECT

    repo = PollerCheckpointRepository(fake_postgres_connection)
    result = repo.get_or_create_checkpoint()

    assert result is None
    assert len(fake_postgres_connection.calls) == 2
    first_query, second_query = (c.query for c in fake_postgres_connection.calls)
    assert "INSERT INTO evaluation_poller_checkpoint" in first_query
    assert "ON CONFLICT (id) DO NOTHING" in first_query
    assert "SELECT last_ingested_at" in second_query


def test_get_or_create_returns_existing_watermark(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=0)
    fake_postgres_connection.queue_result(rows=[(WATERMARK,)])

    repo = PollerCheckpointRepository(fake_postgres_connection)
    assert repo.get_or_create_checkpoint() == WATERMARK


def test_get_or_create_uses_global_singleton_id(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=0)
    fake_postgres_connection.queue_result(rows=[(None,)])

    repo = PollerCheckpointRepository(fake_postgres_connection)
    repo.get_or_create_checkpoint()

    for call in fake_postgres_connection.calls:
        assert call.params["id"] == "global"


def test_advance_checkpoint_uses_optimistic_cas_predicate(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=1)

    repo = PollerCheckpointRepository(fake_postgres_connection)
    repo.advance_checkpoint(observed_watermark=WATERMARK, new_watermark=WATERMARK)

    query = fake_postgres_connection.last_query
    assert "last_ingested_at = %(new_watermark)s" in query
    assert "last_ingested_at = %(observed_watermark)s" in query
    assert "observed_watermark)s IS NULL" in query
    assert "last_ingested_at IS NULL" in query
    assert fake_postgres_connection.last_params == {
        "id": "global",
        "new_watermark": WATERMARK,
        "observed_watermark": WATERMARK,
    }


def test_advance_checkpoint_returns_true_when_one_row_affected(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = PollerCheckpointRepository(fake_postgres_connection)
    assert repo.advance_checkpoint(observed_watermark=None, new_watermark=WATERMARK) is True


def test_advance_checkpoint_returns_false_when_stale(fake_postgres_connection) -> None:
    """A zero-row update means another poller already advanced the
    checkpoint -- not an error, never raised."""
    fake_postgres_connection.queue_result(rowcount=0)
    repo = PollerCheckpointRepository(fake_postgres_connection)
    assert repo.advance_checkpoint(observed_watermark=WATERMARK, new_watermark=WATERMARK) is False


def test_advance_checkpoint_from_none_observed_watermark(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = PollerCheckpointRepository(fake_postgres_connection)
    repo.advance_checkpoint(observed_watermark=None, new_watermark=WATERMARK)

    assert fake_postgres_connection.last_params["observed_watermark"] is None
