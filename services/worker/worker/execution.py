"""Ties together claim -> span fetch -> evaluator dispatch -> result
persistence -> succeeded transition for one already-claimed job.

Critical ordering (docs/decisions/005-evaluation-job-storage-worker.md's
Phase 3 plan section 9): the ClickHouse `evaluation_results` write MUST
complete successfully strictly before the PostgreSQL `succeeded` transition
is even attempted -- never the reverse. If the ClickHouse insert raises,
`mark_succeeded` is never reached; the exception propagates to the caller
and the job's PostgreSQL row is left exactly as `claim_jobs` left it
(`running`), untouched by this module, for a not-yet-built failure/retry
layer to decide what happens next. This is deliberately asymmetric: a crash
after a successful ClickHouse write but before the PostgreSQL transition
commits leaves a `running` row a future stuck-job reaper can safely reclaim
and retry (the retried ClickHouse write is a harmless, auto-deduplicated
duplicate under the same `evaluation_id`); the reverse order could mark a
job `succeeded` with no corresponding result ever written, which nothing in
this system would ever detect or repair.

This module makes no retry/backoff/dead-letter decision, does not loop over
claimed jobs, and does not construct any of its dependencies itself (no
`ThreadPoolExecutor`, no daemon, no composition/wiring) -- all later-phase
concerns. A missing span or an unregistered evaluator each raise a specific,
typed exception and leave the job's PostgreSQL row untouched for that future
failure-handling layer to decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.types import EvaluationResult

from worker.adapters import span_to_relevance_input
from worker.clickhouse.repository import EvaluationResultsRepository
from worker.clickhouse.span_repository import SourceSpanRepository
from worker.postgres.evaluator_config_repository import EvaluatorConfigRepository
from worker.postgres.repository import ClaimedJob, EvaluationJobsRepository
from worker.registry import EvaluatorRegistry
from worker.result_mapping import evaluation_result_to_row
from worker.timeouts import run_with_timeout

DEFAULT_EVALUATOR_CALL_TIMEOUT_SECONDS = 30.0


class SourceSpanNotFoundError(LookupError):
    """The span a claimed job references does not exist in ClickHouse right
    now (e.g. past its retention TTL, or -- despite ADR 005 section 9's
    ground-truth check at job-creation time -- never actually written). Not
    retryable by re-fetching; a future failure-handling layer decides this
    job's fate, not this module.
    """

    def __init__(self, *, project_id: UUID, trace_id: str, span_id: str, job_id: UUID) -> None:
        super().__init__(
            f"No span found for project_id={project_id}, trace_id={trace_id!r}, "
            f"span_id={span_id!r} (job {job_id})."
        )
        self.project_id = project_id
        self.trace_id = trace_id
        self.span_id = span_id
        self.job_id = job_id


@dataclass(frozen=True)
class EvaluationOutcome:
    """What `execute_job` produced on success: the raw `EvaluationResult`
    the evaluator computed, and whether `mark_succeeded` actually took
    effect (`False` means this attempt was stale -- see
    `EvaluationJobsRepository.mark_succeeded`'s docstring -- the ClickHouse
    result row still exists and is correct; only the PostgreSQL side effect
    was a no-op).
    """

    result: EvaluationResult
    marked_succeeded: bool


def execute_job(
    job: ClaimedJob,
    *,
    registry: EvaluatorRegistry,
    span_repository: SourceSpanRepository,
    evaluator_config_repository: EvaluatorConfigRepository,
    results_repository: EvaluationResultsRepository,
    jobs_repository: EvaluationJobsRepository,
    evaluator_call_timeout_seconds: float = DEFAULT_EVALUATOR_CALL_TIMEOUT_SECONDS,
) -> EvaluationOutcome:
    """Run one claimed job to completion. Every dependency is injected --
    this function has no opinion on how its repositories/registry were
    constructed or how many jobs are run this way; that composition belongs
    to `worker.dispatcher.Dispatcher`.

    Order, and why each step comes before the next: resolve the evaluator
    first (usually a pure in-memory dict lookup; on a cold cache, this
    can also lazily construct it -- see `worker.registry.EvaluatorRegistry`,
    which bounds that with its own, separate, more generous
    `evaluator_init_timeout_seconds` -- so an unregistered or slow-to-load
    evaluator is detected before either database is ever touched); fetch
    the span next (so a missing span is detected before any PostgreSQL
    access); look up the project's configured threshold only once a span to
    evaluate actually exists; evaluate (bounded by
    `evaluator_call_timeout_seconds` -- see `worker.timeouts.run_with_timeout`
    for exactly what a timeout does and does not do); persist the result to
    ClickHouse; only then mark the job succeeded in PostgreSQL.

    `evaluator_call_timeout_seconds` wraps *only* the `evaluator.evaluate(...)`
    call below -- never ClickHouse or PostgreSQL I/O, and the two are not in
    the same position with respect to that. ClickHouse calls (`span_repository
    .get_span`, `results_repository.insert_results`) are bounded --
    `worker/clickhouse/client.py` configures both `connect_timeout` and
    `send_receive_timeout` (the latter covers query/data-transfer time, not
    only the initial handshake) from `clickhouse_timeout_seconds`. PostgreSQL
    calls (`evaluator_config_repository.get_config`, `jobs_repository
    .mark_succeeded`, and -- one level up, in `worker.dispatcher.Dispatcher
    ._handle_failure` -- `mark_failed`/`mark_dead_letter`) are now bounded
    the same way (Phase 4D): `worker/postgres/client.py`'s `get_connection()`
    sets both libpq's `connect_timeout` and PostgreSQL's own server-side
    `statement_timeout` from `database_timeout_seconds` -- see that module's
    docstring for exactly what each bounds and the one residual,
    network-partition-specific edge case neither can fully close. A stuck
    PostgreSQL call (lock contention, a slow query) can therefore no longer
    block whichever thread is running `execute_job` -- the outer thread
    inside `Dispatcher`'s `ThreadPoolExecutor`, not one of `worker.timeouts
    .run_with_timeout`'s daemon threads -- indefinitely; it now raises within
    roughly `database_timeout_seconds` instead. This closes the gap this
    docstring named as real and unaddressed through Phase 4A (which was
    scoped to evaluator-call and evaluator-construction timeouts only, not a
    general I/O timeout audit).

    A timeout from the evaluate() call raises `worker.timeouts
    .EvaluatorTimeoutError`, which this function does not catch -- it
    propagates to the caller (`worker.dispatcher.Dispatcher`) exactly like
    `SourceSpanNotFoundError`, `UnknownEvaluatorError`, or any other
    exception this function can raise, and is classified/retried by the
    existing, unmodified `worker.failure_handling` machinery (it is not one
    of `worker.failure_handling`'s two permanent-exception types, so it is
    retryable by default, same as any other unclassified failure).
    """
    evaluator = registry.get(job.evaluator_name, job.evaluator_version)

    span = span_repository.get_span(
        project_id=job.project_id, trace_id=job.trace_id, span_id=job.span_id
    )
    if span is None:
        raise SourceSpanNotFoundError(
            project_id=job.project_id, trace_id=job.trace_id, span_id=job.span_id, job_id=job.id
        )
    evaluator_input = span_to_relevance_input(span)

    config = evaluator_config_repository.get_config(
        project_id=job.project_id, evaluator_name=job.evaluator_name
    )
    threshold = config.threshold if config is not None else None

    result = run_with_timeout(
        lambda: evaluator.evaluate(evaluator_input, threshold=threshold),
        timeout_seconds=evaluator_call_timeout_seconds,
    )

    row = evaluation_result_to_row(result, job)
    results_repository.insert_results([row])

    marked_succeeded = jobs_repository.mark_succeeded(
        job_id=job.id, claimed_attempt_count=job.attempt_count
    )
    return EvaluationOutcome(result=result, marked_succeeded=marked_succeeded)
