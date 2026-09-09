"""Bounded-concurrency dispatch layer over `worker.execution.execute_job`.

Turns a list of already-claimed jobs into a bounded set of concurrent
`execute_job` calls and collects one structured outcome per job. This layer
does not claim jobs (its input is already `list[ClaimedJob]`, produced by
`EvaluationJobsRepository.claim_jobs` elsewhere -- not yet wired together),
does not decide what a failure means (no retry/backoff calculation, no
`mark_failed`/`mark_dead_letter` call -- both remain exclusively
`execute_job`'s and, eventually, a not-yet-built failure-management layer's
concern), and does not duplicate `execute_job`'s own
evaluate -> ClickHouse-write -> PostgreSQL-succeeded ordering. `Dispatcher`
only coordinates *how many* `execute_job` calls run at once and *what
happened* to each one.

Concurrency: a plain `concurrent.futures.ThreadPoolExecutor(max_workers=
max_concurrent_evaluations)`, per docs/decisions/005-evaluation-job-storage-worker.md
section 6/12's "bounded concurrency" requirement -- never unbounded, and the
bound is always an explicit, required constructor argument (see
`worker/config.py`'s `max_concurrent_evaluations` setting for where the
number itself lives). A thread pool, not `asyncio`, is the right primitive
here for the same reason the Phase 3 plan already gives: `evaluate()` is
CPU-bound synchronous work (TF-IDF/ONNX inference), not I/O-bound, so there
is no `async` benefit -- bounded concurrency is just "at most N threads
calling `execute_job` at once."

Two different thread-safety stories, deliberately not conflated:

- **`EvaluatorRegistry`** (and its registered evaluator instances) IS shared
  across every concurrent task, by design -- it is constructed once, holds
  no mutable state after construction (`registered_keys`/`get` are pure
  dict lookups), and each evaluator instance is documented safe for
  concurrent `evaluate()` calls. Nothing about bounded concurrency requires
  -- or should cause -- a new evaluator instance per task.
- **ClickHouse/PostgreSQL resources are the opposite**: a real
  `clickhouse_connect.Client` or `psycopg.Connection` is *not* safe for
  concurrent use from multiple threads, so `Dispatcher` never holds a fixed
  repository instance built outside the pool. Instead it holds a
  `worker.resources.ResourceProvider` -- a zero-argument context-manager
  factory -- and calls it *inside* each task, right before `execute_job`,
  so every task's repositories are backed by resources resolved at that
  exact point (see `worker/resources.py` for how the production provider
  resolves them: a thread-cached ClickHouse client, a freshly opened and
  closed-on-exit PostgreSQL connection). `Dispatcher` itself never imports
  `clickhouse_connect`, `psycopg`, or any repository class -- it has no
  opinion on *why* a provider does what it does, only that calling it
  yields something shaped like `ExecutionResources`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from worker.execution import EvaluationOutcome, execute_job
from worker.postgres.repository import ClaimedJob
from worker.registry import EvaluatorRegistry
from worker.resources import ResourceProvider


@dataclass(frozen=True)
class DispatchOutcome:
    """One claimed job's dispatch result. Exactly one of `evaluation_outcome`
    / `error` is set -- never both, never neither. A set `error` means
    `execute_job` raised (a missing span, an unregistered evaluator, a
    ClickHouse failure, or anything else); this dispatcher does not
    interpret *what kind* of failure it was or decide what should happen
    next -- it only captures it. The job's PostgreSQL row is left exactly
    as `execute_job` left it: `mark_failed`/`mark_dead_letter` are never
    called from here.
    """

    job: ClaimedJob
    evaluation_outcome: EvaluationOutcome | None
    error: Exception | None

    @property
    def succeeded(self) -> bool:
        return self.error is None


class Dispatcher:
    """Constructed once with `max_concurrent_evaluations`, a shared
    `EvaluatorRegistry`, and a `resource_provider`; `dispatch(jobs)` may be
    called repeatedly (e.g. once per claimed batch, by a not-yet-built
    poller/main loop) and always reuses the same registry across every call
    and every job, while resolving a fresh per-task resource bundle for
    each one (see module docstring).
    """

    def __init__(
        self,
        *,
        max_concurrent_evaluations: int,
        registry: EvaluatorRegistry,
        resource_provider: ResourceProvider,
    ) -> None:
        if max_concurrent_evaluations < 1:
            raise ValueError(
                f"max_concurrent_evaluations must be >= 1, got {max_concurrent_evaluations!r}."
            )
        self._max_concurrent_evaluations = max_concurrent_evaluations
        self._registry = registry
        self._resource_provider = resource_provider

    def dispatch(self, jobs: list[ClaimedJob]) -> list[DispatchOutcome]:
        """Run every job in `jobs` through `execute_job`, at most
        `max_concurrent_evaluations` at a time, and return one
        `DispatchOutcome` per job in the same order `jobs` was given --
        regardless of which job actually finished first. Each job's own
        `attempt_count` (captured at claim time, unchanged since) is passed
        through to `execute_job` untouched; this method never re-reads or
        recomputes it, preserving the stale-attempt fencing invariant
        `mark_succeeded` relies on.
        """
        if not jobs:
            return []

        with ThreadPoolExecutor(max_workers=self._max_concurrent_evaluations) as executor:
            futures = [executor.submit(self._run_one, job) for job in jobs]
            return [future.result() for future in futures]

    def _run_one(self, job: ClaimedJob) -> DispatchOutcome:
        try:
            with self._resource_provider() as resources:
                outcome = execute_job(
                    job,
                    registry=self._registry,
                    span_repository=resources.span_repository,
                    evaluator_config_repository=resources.evaluator_config_repository,
                    results_repository=resources.results_repository,
                    jobs_repository=resources.jobs_repository,
                )
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: one job's
            # failure (of any kind execute_job -- or resource acquisition
            # itself -- can raise) must never cancel or corrupt any other
            # concurrently-dispatched job; it is captured here as a
            # structured outcome instead of propagating.
            return DispatchOutcome(job=job, evaluation_outcome=None, error=exc)
        return DispatchOutcome(job=job, evaluation_outcome=outcome, error=None)
