"""Per-task resource ownership boundary for `worker.execution.execute_job`.

`worker.dispatcher.Dispatcher` runs many jobs concurrently via a
`ThreadPoolExecutor`. Neither a real `clickhouse_connect.Client` nor a real
`psycopg.Connection` is safe for concurrent use from more than one thread at
once (see `worker/clickhouse/client.py`'s own docstring for the ClickHouse
case -- a shared client raises "Attempt to execute concurrent queries within
the same session"; a shared PostgreSQL connection has the identical
constraint for the same reason, just without as explicit an error). Binding
a fixed client/connection to a repository *once, outside the pool* and then
sharing that repository across every concurrently-running task is therefore
unsafe -- regardless of how carefully `Dispatcher` itself is written.

The fix is where resource acquisition happens, not a new pool: this module
defines `ExecutionResources` (the bundle `execute_job` needs) and
`ResourceProvider` (a zero-argument context-manager factory that produces
one such bundle). `real_execution_resources`, this module's production
implementation, is called *inside* each worker thread, at task-run time --
never before the thread pool exists:

- ClickHouse: `worker.clickhouse.client.get_clickhouse_client()` already
  caches one client **per calling thread** (`threading.local`, unchanged by
  this module). Calling it from inside each task is what actually makes
  that cache take effect across a pool thread's many jobs; no code in that
  module needed to change.
- PostgreSQL: `worker.postgres.client.get_connection()` already returns a
  brand-new connection on every call (no caching, no pool). Calling it
  fresh per task already gives every task its own connection; this module
  only adds closing it in a `finally`, so opening one per task never leaks
  connections. This is deliberately not a connection pool -- just
  open-use-close, matching the "no pool yet" scope of this fix.

`worker.dispatcher.Dispatcher` depends only on `ExecutionResources`/
`ResourceProvider` (see that module) -- it never imports `clickhouse_connect`,
`psycopg`, or any repository class directly, and has no idea *why* a given
provider does what it does. Production wiring (not yet built -- a worker
main loop) passes `real_execution_resources` to `Dispatcher`; tests pass a
provider that yields a fixed bundle of fakes via `contextlib.nullcontext`
(fakes have no thread-unsafe resource to protect, so reusing one fake bundle
across every concurrently-dispatched task in a test is fine).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from worker.clickhouse.client import get_clickhouse_client
from worker.clickhouse.repository import EvaluationResultsRepository
from worker.clickhouse.span_repository import SourceSpanRepository
from worker.postgres.client import get_connection
from worker.postgres.evaluator_config_repository import EvaluatorConfigRepository
from worker.postgres.repository import EvaluationJobsRepository


@dataclass(frozen=True)
class ExecutionResources:
    """Everything `execute_job` needs for exactly one job, resolved fresh
    (or thread-cached, for ClickHouse) at the point of use -- never a bundle
    shared across concurrently-executing tasks.
    """

    span_repository: SourceSpanRepository
    evaluator_config_repository: EvaluatorConfigRepository
    results_repository: EvaluationResultsRepository
    jobs_repository: EvaluationJobsRepository


# A zero-argument callable returning a context manager that yields one
# `ExecutionResources` bundle and handles its own cleanup on exit. `Dispatcher`
# depends only on this shape.
ResourceProvider = Callable[[], AbstractContextManager[ExecutionResources]]


@contextmanager
def real_execution_resources() -> Iterator[ExecutionResources]:
    """Production `ResourceProvider`. Call this -- not the classes it wraps
    -- from inside the worker thread that is about to run one job, e.g.:

        with real_execution_resources() as resources:
            execute_job(job, registry=registry, **resources_as_kwargs)

    (`Dispatcher` does exactly this internally; nothing else needs to call
    this directly yet, since the main loop that would is not built.)
    """
    clickhouse_client = get_clickhouse_client()
    connection = get_connection()
    try:
        yield ExecutionResources(
            span_repository=SourceSpanRepository(clickhouse_client),
            results_repository=EvaluationResultsRepository(clickhouse_client),
            evaluator_config_repository=EvaluatorConfigRepository(connection),
            jobs_repository=EvaluationJobsRepository(connection),
        )
    finally:
        connection.close()
