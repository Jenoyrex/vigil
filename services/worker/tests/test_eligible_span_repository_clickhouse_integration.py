"""Real-ClickHouse integration tests for
worker.clickhouse.eligible_span_repository.EligibleSpanRepository.

Mirrors test_evaluation_results_clickhouse_integration.py's structure and
skip-if-unreachable convention. Inserts directly into the real `spans`
table (the minimal required-column set per
infrastructure/clickhouse/init/001_create_spans_table.sql), since
`SpansRepository` lives in apps/api, a separate service/venv this package
must not depend on (ADR 001 decision 6).

Uses fixed, obviously-fake project_id/trace_id values scoped per test via a
unique `resource` tag, so repeated runs don't interfere with each other
(matching test_evaluation_results_clickhouse_integration.py's own
ReplacingMergeTree-dedup-tolerant convention, adapted since `spans` has no
natural per-test dedup key as convenient as evaluation_id).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from worker.clickhouse.eligible_span_repository import EligibleSpanRepository

TEST_PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def real_clickhouse_client():
    from worker.clickhouse.client import get_clickhouse_client
    from worker.config import settings

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


_SPAN_COLUMNS = (
    "project_id",
    "trace_id",
    "span_id",
    "name",
    "span_type",
    "resource",
    "start_time",
    "end_time",
    "environment",
    "ingested_at",
)


def _insert_span(
    ch_client,
    *,
    resource: str,
    span_type: str = "llm",
    trace_id: str | None = None,
    span_id: str | None = None,
    ingested_at: datetime,
    start_time: datetime | None = None,
) -> tuple[str, str]:
    trace_id = trace_id or uuid.uuid4().hex
    span_id = span_id or uuid.uuid4().hex[:16]
    start_time = start_time or ingested_at
    ch_client.insert(
        "spans",
        [
            [
                TEST_PROJECT_ID,
                trace_id,
                span_id,
                "test-span",
                span_type,
                resource,
                start_time,
                start_time,
                "test",
                ingested_at,
            ]
        ],
        column_names=list(_SPAN_COLUMNS),
    )
    return trace_id, span_id


def test_llm_span_type_is_selected(real_clickhouse_client) -> None:
    resource = f"test-{uuid.uuid4().hex[:8]}"
    ingested_at = datetime.now(UTC)
    trace_id, span_id = _insert_span(
        real_clickhouse_client, resource=resource, span_type="llm", ingested_at=ingested_at
    )

    repo = EligibleSpanRepository(real_clickhouse_client)
    spans = repo.select_eligible_spans(
        lower_bound=ingested_at - timedelta(seconds=1),
        start_date_hint=None,
        batch_size=100,
    )

    matching = [s for s in spans if s.trace_id == trace_id and s.span_id == span_id]
    assert len(matching) == 1


def test_non_llm_span_type_is_excluded(real_clickhouse_client) -> None:
    resource = f"test-{uuid.uuid4().hex[:8]}"
    ingested_at = datetime.now(UTC)
    trace_id, span_id = _insert_span(
        real_clickhouse_client, resource=resource, span_type="tool", ingested_at=ingested_at
    )

    repo = EligibleSpanRepository(real_clickhouse_client)
    spans = repo.select_eligible_spans(
        lower_bound=ingested_at - timedelta(seconds=1),
        start_date_hint=None,
        batch_size=100,
    )

    matching = [s for s in spans if s.trace_id == trace_id and s.span_id == span_id]
    assert matching == []


def test_deterministic_ordering_by_ingested_at_then_span_id(real_clickhouse_client) -> None:
    """Same-ingested_at rows must still come back in a stable, reproducible
    order (by span_id) -- proving the query is safe to re-run identically
    after a crash-before-checkpoint-advance, per worker/poller.py."""
    shared_ingested_at = datetime.now(UTC)
    span_ids = sorted(uuid.uuid4().hex[:16] for _ in range(5))
    resource = f"test-{uuid.uuid4().hex[:8]}"
    for span_id in span_ids:
        _insert_span(
            real_clickhouse_client,
            resource=resource,
            ingested_at=shared_ingested_at,
            span_id=span_id,
        )

    repo = EligibleSpanRepository(real_clickhouse_client)
    first_run = repo.select_eligible_spans(
        lower_bound=shared_ingested_at - timedelta(seconds=1),
        start_date_hint=None,
        batch_size=1000,
    )
    second_run = repo.select_eligible_spans(
        lower_bound=shared_ingested_at - timedelta(seconds=1),
        start_date_hint=None,
        batch_size=1000,
    )

    first_ids = [s.span_id for s in first_run if s.span_id in span_ids]
    second_ids = [s.span_id for s in second_run if s.span_id in span_ids]
    assert first_ids == span_ids  # sorted order, matching ORDER BY ... span_id
    assert first_ids == second_ids  # identical across repeated identical queries


def test_same_ingested_at_timestamps_are_all_discoverable_together(real_clickhouse_client) -> None:
    """Reproduces the collision this whole design accounts for: multiple
    spans sharing one exact ingested_at (the common case for a batched
    ClickHouse insert, since now64() is evaluated once per INSERT
    statement, not once per row -- this test constructs it explicitly
    rather than relying on that timing behavior). All of them must be
    discoverable by one lower_bound query, none silently dropped."""
    shared_ingested_at = datetime.now(UTC)
    resource = f"test-{uuid.uuid4().hex[:8]}"
    span_ids = {uuid.uuid4().hex[:16] for _ in range(4)}
    for span_id in span_ids:
        _insert_span(
            real_clickhouse_client,
            resource=resource,
            ingested_at=shared_ingested_at,
            span_id=span_id,
        )

    repo = EligibleSpanRepository(real_clickhouse_client)
    spans = repo.select_eligible_spans(
        lower_bound=shared_ingested_at - timedelta(seconds=1),
        start_date_hint=None,
        batch_size=1000,
    )

    found_ids = {s.span_id for s in spans if s.span_id in span_ids}
    assert found_ids == span_ids


def test_overlap_lower_bound_rediscovers_a_span_at_the_prior_watermark(
    real_clickhouse_client,
) -> None:
    """Simulates worker/poller.py's overlap-window strategy directly: a
    span with ingested_at exactly at a prior checkpoint value is missed by
    a naive `ingested_at > checkpoint` (strict >), but IS discovered once
    the caller subtracts poller_overlap_seconds before querying -- the
    mechanism this whole design relies on instead of a composite cursor."""
    watermark = datetime.now(UTC)
    resource = f"test-{uuid.uuid4().hex[:8]}"
    trace_id, span_id = _insert_span(
        real_clickhouse_client, resource=resource, ingested_at=watermark
    )

    repo = EligibleSpanRepository(real_clickhouse_client)

    naive = repo.select_eligible_spans(lower_bound=watermark, start_date_hint=None, batch_size=1000)
    assert not any(s.span_id == span_id for s in naive)  # strict > excludes the exact watermark

    with_overlap = repo.select_eligible_spans(
        lower_bound=watermark - timedelta(seconds=60), start_date_hint=None, batch_size=1000
    )
    assert any(s.trace_id == trace_id and s.span_id == span_id for s in with_overlap)


def test_start_time_pruning_predicate_present_but_never_relied_on_for_correctness(
    real_clickhouse_client,
) -> None:
    """start_date_hint is a partition-pruning OPTIMIZATION ONLY -- proves
    it doesn't silently exclude a span whose ingested_at is within the
    scanned window even when start_date_hint is applied, as long as the
    hint's date range is generous enough to include the span's start_time
    (exactly how worker/poller.py derives it -- see _compute_scan_window).
    This does not test "correctness with no hint" (that's every other test
    in this file, which pass start_date_hint=None); it tests that adding the
    hint doesn't change the result when it correctly covers the span.
    """
    now = datetime.now(UTC)
    resource = f"test-{uuid.uuid4().hex[:8]}"
    trace_id, span_id = _insert_span(
        real_clickhouse_client, resource=resource, ingested_at=now, start_time=now
    )

    repo = EligibleSpanRepository(real_clickhouse_client)
    spans = repo.select_eligible_spans(
        lower_bound=now - timedelta(seconds=1),
        start_date_hint=date.today() - timedelta(days=1),
        batch_size=1000,
    )

    assert any(s.trace_id == trace_id and s.span_id == span_id for s in spans)
