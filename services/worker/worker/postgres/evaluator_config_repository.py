"""Read-only access to a project's per-evaluator configuration
(`evaluator_configs`), for exactly one purpose in this phase: resolving the
effective threshold to pass into an evaluator's per-call `threshold`
argument (docs/decisions/005-evaluation-job-storage-worker.md's Phase 3
threshold-resolution amendment).

Deliberately does NOT decide whether a job should exist -- `enabled`/
`sampling_rate` gating is `apps/api`'s job-creation-time business logic
(ADR 005 section 10), already applied before this job's row ever existed.
This repository only reads the one config row a claimed job's own
`(project_id, evaluator_name)` already point at; it never writes, and no
config-mutation API exists here or is planned here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

_GET_CONFIG_SQL = """
    SELECT enabled, sampling_rate, threshold
    FROM evaluator_configs
    WHERE project_id = %(project_id)s AND evaluator_name = %(evaluator_name)s
"""


@dataclass(frozen=True)
class EvaluatorConfig:
    enabled: bool
    sampling_rate: float
    threshold: float | None


class EvaluatorConfigRepository:
    def __init__(self, connection) -> None:
        self._connection = connection

    def get_config(self, *, project_id: uuid.UUID, evaluator_name: str) -> EvaluatorConfig | None:
        """`None` if no `evaluator_configs` row exists for this
        `(project_id, evaluator_name)` pair -- degenerate in production
        (a job only exists because `apps/api`'s job-creation endpoint
        already found an enabled config row for it), but handled the same
        way a present row with `threshold IS NULL` is: the caller resolves
        the effective threshold as `None`, meaning "use this evaluator's
        own code-level default" (`evaluator_configs.threshold`'s documented
        NULL semantics, unchanged by this repository).
        """
        cursor = self._connection.execute(
            _GET_CONFIG_SQL, {"project_id": project_id, "evaluator_name": evaluator_name}
        )
        row = cursor.fetchone()
        if row is None:
            return None
        enabled, sampling_rate, threshold = row
        return EvaluatorConfig(enabled=enabled, sampling_rate=sampling_rate, threshold=threshold)
