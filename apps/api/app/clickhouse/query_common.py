"""Shared read-query execution helper for the Trace Explorer/analytics
repositories (app/clickhouse/query_repository.py,
app/clickhouse/analytics_repository.py).

Every query executed through `execute_query` uses ClickHouse's native
server-side parameter binding (`{name:Type}` placeholders + a `parameters`
dict) -- values are never string-interpolated into the query text, so a
value can never break out of its placeholder regardless of content (verified
against clickhouse_connect 1.7.2: a value containing quotes/SQL syntax is
bound as a literal value, not parsed as SQL). Column/function names that
vary per request (e.g. which `group_by` dimension) are selected from a
fixed Python-side allow-list before ever reaching a query string -- see
`_safe_identifier` in the two repository modules -- since ClickHouse's
parameter binding only binds values, never identifiers.
"""

from __future__ import annotations

import logging
from typing import Any

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

from app.clickhouse.repository import ClickHouseUnavailableError

logger = logging.getLogger(__name__)

__all__ = ["ClickHouseQueryError", "ClickHouseUnavailableError", "execute_query"]


class ClickHouseQueryError(RuntimeError):
    """ClickHouse was reachable but rejected or failed a read query."""


def execute_query(client: Client, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        result = client.query(query, parameters=parameters)
    except OperationalError as exc:
        logger.error("ClickHouse unavailable during query: %s", exc)
        raise ClickHouseUnavailableError(str(exc)) from exc
    except ClickHouseError as exc:
        logger.error("ClickHouse query failed: %s", exc)
        raise ClickHouseQueryError(str(exc)) from exc
    return list(result.named_results())
