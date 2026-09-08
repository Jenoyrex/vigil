"""Shared fixtures for services/worker tests.

`FakeClickHouseClient` mirrors apps/api/tests/conftest.py's `FakeChQueryClient`
(for `.query(...)`) and its `FakeSpansRepository`-adjacent insert-recording
pattern (for `.insert(...)`), combined into one fake since
`EvaluationResultsRepository` is the one class that needs both -- a real
`clickhouse_connect.Client` exposes both methods on the same object, so the
fake does too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class FakeChResult:
    """Enough of clickhouse_connect's QueryResult interface for
    EvaluationResultsRepository.get_by_evaluation_id: `named_results()`."""

    def __init__(self, column_names: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.column_names = column_names
        self.result_rows = rows

    def named_results(self):
        for row in self.result_rows:
            yield dict(zip(self.column_names, row, strict=True))


class FakeClickHouseClient:
    """Fake clickhouse_connect Client for EvaluationResultsRepository tests:
    records every `.insert(...)` call (so tests can assert exact column
    order without a real server) and every `.query(query, parameters=...)`
    call (so tests can assert the exact generated SQL and bound
    parameters -- tenant scoping, FINAL presence, etc.), returning a
    scripted response for the latter.
    """

    def __init__(self) -> None:
        self.insert_calls: list[SimpleNamespace] = []
        self.query_calls: list[SimpleNamespace] = []
        self._query_responses: list[FakeChResult] = []
        self.fail_with: Exception | None = None

    def queue_result(self, column_names: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self._query_responses.append(FakeChResult(column_names, rows))

    def insert(self, table: str, data: list[list[Any]], column_names: list[str], **_: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.insert_calls.append(SimpleNamespace(table=table, data=data, column_names=column_names))

    def query(self, query: str, parameters: dict[str, Any] | None = None, **_: Any) -> FakeChResult:
        self.query_calls.append(SimpleNamespace(query=query, parameters=parameters or {}))
        if self.fail_with is not None:
            raise self.fail_with
        if self._query_responses:
            return self._query_responses.pop(0)
        return FakeChResult((), [])

    @property
    def last_insert(self) -> SimpleNamespace:
        return self.insert_calls[-1]

    @property
    def last_query(self) -> str:
        return self.query_calls[-1].query

    @property
    def last_parameters(self) -> dict[str, Any]:
        return self.query_calls[-1].parameters


@pytest.fixture
def fake_clickhouse_client() -> FakeClickHouseClient:
    return FakeClickHouseClient()


class FakePostgresCursor:
    """Enough of a psycopg Cursor for EvaluationJobsRepository/
    EvaluatorConfigRepository: `fetchall()`, `fetchone()`, and `rowcount`."""

    def __init__(self, rows: list[tuple[Any, ...]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakePostgresConnection:
    """Fake psycopg Connection for EvaluationJobsRepository/
    EvaluatorConfigRepository unit tests:
    records every `.execute(query, params)` call (so tests can assert the
    exact generated SQL and bound parameters -- the attempt_count fencing
    clause, in particular -- without a real server) and returns a scripted
    `(rows, rowcount)` response. Mirrors `FakeChQueryClient`'s
    call-recording pattern, adapted to psycopg's `Connection.execute`
    shape rather than clickhouse_connect's `Client.query`.
    """

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self._responses: list[tuple[list[tuple[Any, ...]], int]] = []
        self.fail_with: Exception | None = None

    def queue_result(self, rows: list[tuple[Any, ...]] = (), rowcount: int = 0) -> None:
        self._responses.append((list(rows), rowcount))

    def execute(self, query: str, params: dict[str, Any] | None = None) -> FakePostgresCursor:
        self.calls.append(SimpleNamespace(query=query, params=params or {}))
        if self.fail_with is not None:
            raise self.fail_with
        rows, rowcount = self._responses.pop(0) if self._responses else ([], 0)
        return FakePostgresCursor(rows, rowcount)

    @property
    def last_query(self) -> str:
        return self.calls[-1].query

    @property
    def last_params(self) -> dict[str, Any]:
        return self.calls[-1].params


@pytest.fixture
def fake_postgres_connection() -> FakePostgresConnection:
    return FakePostgresConnection()
