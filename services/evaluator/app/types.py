"""Plain, framework-independent data types shared by every Vigil evaluator.

Deliberately has zero dependency on apps/api, services/worker, ClickHouse,
PostgreSQL, or any Vigil-internal model or ORM. An evaluator receives its
own evaluator-specific input type (see e.g. relevance.py's
`RelevanceEvaluatorInput`) and always returns the single shared
`EvaluationResult` defined here -- see interface.py for how the two are
tied together, and docs/decisions/004-evaluation-engine.md section 7 for
the result schema this type implements.
"""

from __future__ import annotations

from dataclasses import dataclass


class InvalidEvaluatorInputError(ValueError):
    """Raised when evaluator input is structurally invalid -- wrong type,
    not merely empty or low-content. Empty/whitespace-only text is a
    valid, well-defined input that an evaluator handles by returning an
    `EvaluationResult` with `label="not_evaluable"` (see relevance.py);
    this exception is reserved for inputs that are not the shape the
    evaluator's contract promises at all (e.g. a non-string value where a
    string is required).
    """


@dataclass(frozen=True)
class EvaluationResult:
    """The output of any evaluator's `evaluate()` call.

    Field set matches docs/decisions/004-evaluation-engine.md section 7,
    minus `evaluation_id`, `project_id`, `trace_id`, `span_id`, and
    `created_at` -- those identify *where* a result belongs once it is
    persisted and associated with a specific span, which is a
    services/worker/storage concern (not yet built -- see that ADR's
    section 12). An evaluator is a pure function over plain text; it has
    no reason to know what trace or span its input came from, so this
    type deliberately excludes any such identifier. Whatever calls an
    evaluator is responsible for attaching those identifiers when it
    persists this result.
    """

    evaluator_name: str
    evaluator_version: str

    # `None` means "not meaningfully computable for this input" (see
    # relevance.py's not-evaluable handling), distinct from a real
    # numeric score of 0.0 -- the same "null is not zero" convention
    # docs/decisions/003-clickhouse-telemetry-storage.md section 2 uses
    # for `llm_cost_usd`.
    score: float | None

    # Always populated, including for a not-evaluable outcome (e.g.
    # "not_evaluable") -- a defined result, not the absence of one.
    label: str

    # Bounded, human-readable justification for score/label. Must never
    # contain a raw, unbounded payload dump of the evaluator's input --
    # see docs/decisions/004-evaluation-engine.md section 8.
    explanation: str

    evaluation_latency_ms: float

    # `None` (not 0.0) for an evaluator with no marginal cost, e.g. a
    # local model -- same null-vs-zero convention as `score` above.
    evaluation_cost_usd: float | None = None

    # Which model computed this result. Populated even for a local,
    # non-LLM model (attribution, not just third-party disclosure).
    evaluator_model: str | None = None

    # Which provider hosts `evaluator_model`. `None` for a local model;
    # set only when an external API was actually called.
    evaluator_provider: str | None = None
