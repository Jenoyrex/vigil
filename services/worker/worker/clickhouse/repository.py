"""Data-access layer for the ClickHouse `evaluation_results` table.

Mirrors `apps/api/app/clickhouse/repository.py` (`SpansRepository`) and
`apps/api/app/clickhouse/query_repository.py`'s conventions exactly --
batched, structured inserts (never one `INSERT` per row), ClickHouse's
native server-side parameter binding for every read (`{name:Type}`
placeholders + a `parameters` dict, values never string-interpolated into
query text), and `FixedString` output columns wrapped in `toString(...)`
because `clickhouse_connect` decodes `FixedString` as raw `bytes`, not
`str`. Deliberately a fresh implementation rather than an import of either
apps/api module: services/worker must not depend on apps/api, per ADR 001
decision 6 ("duplicate rather than centralize"), and this repository has no
reason to import `services/evaluator` either -- it operates on plain dicts,
not `EvaluationResult` instances, keeping the evaluator package's dataclass
decoupled from ClickHouse entirely. Whatever calls this repository (the
future dispatch/persistence code, not yet built) is responsible for turning
one `EvaluationResult` plus its job identity into one such row dict.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

logger = logging.getLogger(__name__)

# Column order for batch inserts. Deliberately excludes `written_at`: it has
# `DEFAULT now64(3)` (infrastructure/clickhouse/init/002_create_evaluation_results_table.sql),
# so omitting it from the insert's column list lets ClickHouse apply that
# default per-row -- exactly SpansRepository's precedent for `ingested_at`.
# See docs/decisions/005-evaluation-job-storage-worker.md section 5 and that
# init script -- do not add columns here without a matching schema change
# approved there first.
RESULT_COLUMNS: tuple[str, ...] = (
    "evaluation_id",
    "project_id",
    "trace_id",
    "span_id",
    "evaluator_name",
    "evaluator_version",
    "score",
    "label",
    "explanation",
    "evaluator_model",
    "evaluator_provider",
    "evaluation_latency_ms",
    "evaluation_cost_usd",
    "job_created_at",
)

# Output columns for the point-lookup below. `trace_id`/`span_id` need the
# same `toString(...)` treatment app/clickhouse/query_repository.py already
# documents; `evaluation_id`/`project_id` are plain `UUID` columns and need
# no cast.
_RESULT_SELECT_COLUMNS = (
    "evaluation_id",
    "project_id",
    "toString(trace_id) AS trace_id",
    "toString(span_id) AS span_id",
    "evaluator_name",
    "evaluator_version",
    "score",
    "label",
    "explanation",
    "evaluator_model",
    "evaluator_provider",
    "evaluation_latency_ms",
    "evaluation_cost_usd",
    "job_created_at",
    "written_at",
)
_RESULT_SELECT = ",\n                ".join(_RESULT_SELECT_COLUMNS)


class ClickHouseUnavailableError(RuntimeError):
    """ClickHouse could not be reached at all (connection/timeout failure)."""


class ClickHouseInsertError(RuntimeError):
    """ClickHouse was reachable but rejected or failed an insert."""


class ClickHouseQueryError(RuntimeError):
    """ClickHouse was reachable but rejected or failed a read query."""


class EvaluationResultsRepository:
    """Batch-inserts and point-looks-up rows in ClickHouse `evaluation_results`.

    Rows passed to `insert_results` are plain dicts keyed by column name
    (see `RESULT_COLUMNS`) -- the same shape `SpansRepository.insert_spans`
    expects, deliberately. `evaluation_id` must be the corresponding
    `evaluation_jobs.id` (PostgreSQL) verbatim: per ADR 005's Phase 2
    decision, this is the one and only identifier ClickHouse uses for a
    result row, and it is never regenerated here -- the caller (the future
    persistence adapter) is responsible for passing it through unchanged.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def insert_results(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return

        data = [[row[column] for column in RESULT_COLUMNS] for row in rows]

        try:
            self._client.insert("evaluation_results", data, column_names=list(RESULT_COLUMNS))
        except OperationalError as exc:
            logger.error("ClickHouse unavailable during evaluation_results insert: %s", exc)
            raise ClickHouseUnavailableError(str(exc)) from exc
        except ClickHouseError as exc:
            logger.error("ClickHouse rejected evaluation_results insert: %s", exc)
            raise ClickHouseInsertError(str(exc)) from exc

    def get_by_evaluation_id(
        self, *, project_id: UUID, evaluation_id: UUID
    ) -> dict[str, Any] | None:
        """The single result row identified by `(project_id, evaluation_id)`
        -- `evaluation_id` alone is already globally unique (it equals the
        owning `evaluation_jobs.id`), but every query in this system scopes
        by `project_id` regardless, matching
        `TracesQueryRepository`'s tenant-scoping convention exactly. Uses
        `FINAL` for the same reason `TracesQueryRepository.get_span` does: a
        single-identity point lookup is cheap enough to afford immediate
        deduplication rather than tolerating ReplacingMergeTree's eventual
        consistency window.
        """
        query = f"""
            SELECT
                {_RESULT_SELECT}
            FROM evaluation_results FINAL
            WHERE project_id = {{project_id:UUID}} AND evaluation_id = {{evaluation_id:UUID}}
            LIMIT 1
        """
        parameters: dict[str, Any] = {"project_id": project_id, "evaluation_id": evaluation_id}

        try:
            result = self._client.query(query, parameters=parameters)
        except OperationalError as exc:
            logger.error("ClickHouse unavailable during evaluation_results query: %s", exc)
            raise ClickHouseUnavailableError(str(exc)) from exc
        except ClickHouseError as exc:
            logger.error("ClickHouse rejected evaluation_results query: %s", exc)
            raise ClickHouseQueryError(str(exc)) from exc

        rows = list(result.named_results())
        return rows[0] if rows else None
