"""Data-access layer for the ClickHouse `spans` table.

Keeps raw ClickHouse concerns (column order, the driver's own exception
hierarchy) out of the route/service layer, which only needs to know about
`SpansRepository` and the two exceptions below.
"""

from __future__ import annotations

import logging
from typing import Any

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

logger = logging.getLogger(__name__)

# Column order for batch inserts. Deliberately excludes:
#   - `duration_ms`: a MATERIALIZED column: ClickHouse computes it, it cannot
#     be inserted into.
#   - `ingested_at`: has `DEFAULT now64(3)`; omitting it from the insert's
#     column list lets ClickHouse apply that default per-row.
# See docs/decisions/003-clickhouse-telemetry-storage.md and
# infrastructure/clickhouse/init/001_create_spans_table.sql -- do not add
# columns here without a matching schema change approved there first.
SPAN_COLUMNS: tuple[str, ...] = (
    "project_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "span_type",
    "resource",
    "start_time",
    "end_time",
    "status",
    "status_message",
    "input",
    "input_size_bytes",
    "input_truncated",
    "output",
    "output_size_bytes",
    "output_truncated",
    "attributes",
    "attributes_truncated",
    "events.time",
    "events.name",
    "events.attributes",
    "events_truncated",
    "llm_provider",
    "llm_model",
    "llm_input_tokens",
    "llm_output_tokens",
    "llm_total_tokens",
    "llm_cost_usd",
    "environment",
    "release",
)


class ClickHouseUnavailableError(RuntimeError):
    """ClickHouse could not be reached at all (connection/timeout failure)."""


class ClickHouseInsertError(RuntimeError):
    """ClickHouse was reachable but rejected or failed the insert."""


class SpansRepository:
    """Batch-inserts span rows into ClickHouse.

    Rows are plain dicts keyed by column name, as produced by
    `app.services.ingestion.transform_request`. Insertion is a single batch
    call -- never one INSERT per span -- via the client's native `insert()`,
    which builds the request from structured column/row data rather than any
    string-concatenated SQL.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def insert_spans(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return

        data = [[row[column] for column in SPAN_COLUMNS] for row in rows]

        try:
            self._client.insert("spans", data, column_names=list(SPAN_COLUMNS))
        except OperationalError as exc:
            logger.error("ClickHouse unavailable during span insert: %s", exc)
            raise ClickHouseUnavailableError(str(exc)) from exc
        except ClickHouseError as exc:
            logger.error("ClickHouse rejected span insert: %s", exc)
            raise ClickHouseInsertError(str(exc)) from exc
