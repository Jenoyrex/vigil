"""Fetches one span's evaluator-relevant fields from ClickHouse `spans`, for
services/worker's claim -> evaluate -> persist execution path
(docs/decisions/005-evaluation-job-storage-worker.md section 5).

Deliberately reuses `worker.clickhouse.repository`'s exception classes
(`ClickHouseUnavailableError`/`ClickHouseQueryError`) rather than duplicating
them -- same failure categories against the same store, exactly the
precedent `apps/api/app/clickhouse/query_common.py` already sets by
importing `ClickHouseUnavailableError` from `repository.py` rather than
redefining it.

Selects only the four fields `worker/adapters.py` needs to build a
`RelevanceEvaluatorInput` -- never `SELECT *` -- scoped by `(project_id,
trace_id, span_id)`, using `FINAL` for the same single-identity-point-lookup
reason `apps/api/app/clickhouse/query_repository.py`'s `get_span` already
documents: a lookup this narrow can afford immediate deduplication rather
than tolerating `ReplacingMergeTree`'s eventual-consistency window. Never
queries PostgreSQL from here -- ClickHouse and PostgreSQL stay two
independent stores, joined only at the application layer (this repository's
caller, `worker/execution.py`, is that join point).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

from worker.clickhouse.repository import ClickHouseQueryError, ClickHouseUnavailableError

logger = logging.getLogger(__name__)

_SOURCE_SPAN_QUERY = """
    SELECT
        input,
        input_truncated,
        output,
        output_truncated
    FROM spans FINAL
    WHERE project_id = {project_id:UUID}
      AND trace_id = {trace_id:String}
      AND span_id = {span_id:String}
    LIMIT 1
"""


class SourceSpanRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get_span(self, *, project_id: UUID, trace_id: str, span_id: str) -> dict[str, Any] | None:
        """The four evaluator-relevant fields of the single span identified
        by `(project_id, trace_id, span_id)`, or `None` if no such span
        exists (e.g. past its retention TTL, or never actually written
        despite a job existing for it -- see `worker/execution.py`'s
        `SourceSpanNotFoundError` for how a caller turns that into a typed
        failure).
        """
        parameters: dict[str, Any] = {
            "project_id": project_id,
            "trace_id": trace_id,
            "span_id": span_id,
        }
        try:
            result = self._client.query(_SOURCE_SPAN_QUERY, parameters=parameters)
        except OperationalError as exc:
            logger.error("ClickHouse unavailable during source span fetch: %s", exc)
            raise ClickHouseUnavailableError(str(exc)) from exc
        except ClickHouseError as exc:
            logger.error("ClickHouse rejected source span fetch: %s", exc)
            raise ClickHouseQueryError(str(exc)) from exc

        rows = list(result.named_results())
        return rows[0] if rows else None
