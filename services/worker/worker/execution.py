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
) -> EvaluationOutcome:
    """Run one claimed job to completion. Every dependency is injected --
    this function has no opinion on how its repositories/registry were
    constructed or how many jobs are run this way; that composition belongs
    to a not-yet-built dispatch loop.

    Order, and why each step comes before the next: resolve the evaluator
    first (a pure in-memory dict lookup -- cheapest possible failure to
    detect, so an unregistered evaluator never touches either database);
    fetch the span next (so a missing span is detected before any
    PostgreSQL access); look up the project's configured threshold only
    once a span to evaluate actually exists; evaluate; persist the result to
    ClickHouse; only then mark the job succeeded in PostgreSQL.
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

    result = evaluator.evaluate(evaluator_input, threshold=threshold)

    row = evaluation_result_to_row(result, job)
    results_repository.insert_results([row])

    marked_succeeded = jobs_repository.mark_succeeded(
        job_id=job.id, claimed_attempt_count=job.attempt_count
    )
    return EvaluationOutcome(result=result, marked_succeeded=marked_succeeded)
