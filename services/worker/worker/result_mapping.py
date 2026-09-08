"""Converts one `EvaluationResult` (`services/evaluator`'s output) plus its
originating `ClaimedJob` (`services/worker`'s PostgreSQL claim) into the
exact row shape `EvaluationResultsRepository.insert_results` expects
(`worker/clickhouse/repository.py`'s `RESULT_COLUMNS`).

`evaluation_id` is always `job.id`, verbatim -- never a freshly generated
uuid, per docs/decisions/005-evaluation-job-storage-worker.md's Phase 2
decision. `job_created_at` is `job.created_at`, the job's own immutable
creation timestamp -- ADR 005 section 5 explains why `evaluation_results`'
partitioning/`ORDER BY` must use this rather than the moment this row is
actually written (a retried job's write must land in the same daily
partition as any earlier attempt, or `ReplacingMergeTree` can never merge
the duplicate away). `written_at` is never populated here -- the
`evaluation_results` table's own `DEFAULT now64(3)` applies it.
"""

from __future__ import annotations

from typing import Any

from app.types import EvaluationResult

from worker.postgres.repository import ClaimedJob


def evaluation_result_to_row(result: EvaluationResult, job: ClaimedJob) -> dict[str, Any]:
    return {
        "evaluation_id": job.id,
        "project_id": job.project_id,
        "trace_id": job.trace_id,
        "span_id": job.span_id,
        "evaluator_name": result.evaluator_name,
        "evaluator_version": result.evaluator_version,
        "score": result.score,
        "label": result.label,
        "explanation": result.explanation,
        "evaluator_model": result.evaluator_model,
        "evaluator_provider": result.evaluator_provider,
        "evaluation_latency_ms": result.evaluation_latency_ms,
        "evaluation_cost_usd": result.evaluation_cost_usd,
        "job_created_at": job.created_at,
    }
