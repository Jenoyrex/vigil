"""Discovers ClickHouse `spans` newly eligible for evaluation-job creation
-- the poller's scan, docs/decisions/005-evaluation-job-storage-worker.md's
Phase 3H amendment.

Selects only the columns `worker/poller.py` needs (`project_id`, `trace_id`,
`span_id`, `ingested_at`) -- never `SELECT *`, matching
`worker/clickhouse/span_repository.py`'s precedent. No `FINAL`: this is a
scan across many rows bounded by a time window, not a single-identity point
lookup -- the same "a list/scan endpoint tolerates `ReplacingMergeTree`'s
eventual-consistency window, only a point lookup needs immediate dedup"
reasoning `app/clickhouse/query_common.py` and `span_repository.py` already
document, and `FINAL` over a wide, unbounded-by-identity range would be
prohibitively expensive besides.

**`start_time >= :start_date_hint` is a partition-pruning OPTIMIZATION
ONLY, never a correctness boundary.** `spans`' ClickHouse `ORDER BY`/
`PARTITION BY` is `(project_id, toDate(start_time), trace_id, span_id)` /
`toDate(start_time)` (`infrastructure/clickhouse/init/001_create_spans_table.sql`)
-- `ingested_at` is not part of that key at all, only the `ReplacingMergeTree`
version column, so a bare `ingested_at > :lower_bound` scan cannot be
index-accelerated by ClickHouse's sparse primary index and is only
partition-pruned by whatever this `start_time` hint additionally provides.
The actual correctness guarantee lives entirely in `ingested_at > :lower_bound`,
the deterministic `(ingested_at, span_id)` ordering, and the overlap window
`worker/poller.py` applies to `lower_bound` -- this predicate exists purely
to keep the query from scanning ClickHouse parts far outside any plausible
recent-ingestion range, and is omitted entirely (see `select_eligible_spans`)
when there is no watermark yet to derive a "recent" hint from, so the very
first poll still correctly scans the whole retention window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

from worker.clickhouse.repository import ClickHouseQueryError, ClickHouseUnavailableError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EligibleSpan:
    project_id: UUID
    trace_id: str
    span_id: str
    ingested_at: datetime


def _as_utc(value: datetime) -> datetime:
    """ClickHouse returns `DateTime64` values as naive Python datetimes (no
    `tzinfo` attached) -- the same behavior
    `apps/api/app/services/query.py`'s identical `_as_utc` helper already
    documents (independently duplicated here, not imported, per ADR 001
    decision 6 -- services/worker must not depend on apps/api). Every
    `ingested_at` value in `spans` is UTC (`DEFAULT now64(3)`), so attach UTC
    explicitly rather than comparing/storing an ambiguous, offset-less
    timestamp against PostgreSQL's `timestamptz` checkpoint column.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class EligibleSpanRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def select_eligible_spans(
        self, *, lower_bound: datetime | None, start_date_hint: date | None, batch_size: int
    ) -> list[EligibleSpan]:
        """Up to `batch_size` `span_type = 'llm'` spans, ordered
        deterministically by `(ingested_at, span_id)` -- no `OFFSET`
        pagination anywhere; the watermark itself (`lower_bound`) is the
        cursor, advanced by the caller only after every candidate in the
        returned batch has a definitive job-creation outcome
        (`worker/poller.py`).

        `lower_bound=None` means "no checkpoint yet -- scan from the
        beginning of the retention window" (`evaluation_poller_checkpoint`'s
        own documented semantics for a `NULL` `last_ingested_at`): the
        `ingested_at` predicate is omitted entirely rather than compared
        against `NULL` (which would match nothing in SQL). `start_date_hint=None`
        is the poller's own way of saying "don't apply the partition-pruning
        optimization this tick" (see module docstring) -- also omitted
        entirely, not compared.
        """
        conditions = ["span_type = 'llm'"]
        parameters: dict[str, Any] = {"batch_size": batch_size}
        if lower_bound is not None:
            conditions.append("ingested_at > {lower_bound:DateTime64(3)}")
            parameters["lower_bound"] = lower_bound
        if start_date_hint is not None:
            conditions.append("start_time >= {start_date_hint:Date}")
            parameters["start_date_hint"] = start_date_hint
        where_sql = " AND ".join(conditions)

        query = f"""
            SELECT
                project_id,
                toString(trace_id) AS trace_id,
                toString(span_id) AS span_id,
                ingested_at
            FROM spans
            WHERE {where_sql}
            ORDER BY ingested_at, span_id
            LIMIT {{batch_size:UInt32}}
        """

        try:
            result = self._client.query(query, parameters=parameters)
        except OperationalError as exc:
            logger.error("ClickHouse unavailable during eligible-span scan: %s", exc)
            raise ClickHouseUnavailableError(str(exc)) from exc
        except ClickHouseError as exc:
            logger.error("ClickHouse rejected eligible-span scan: %s", exc)
            raise ClickHouseQueryError(str(exc)) from exc

        return [
            EligibleSpan(
                project_id=row["project_id"],
                trace_id=row["trace_id"],
                span_id=row["span_id"],
                ingested_at=_as_utc(row["ingested_at"]),
            )
            for row in result.named_results()
        ]
