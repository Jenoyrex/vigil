"""Converts a fetched ClickHouse span row into evaluator-specific input
types. Currently the only shape needed: span -> `RelevanceEvaluatorInput`,
shared by both registered relevance evaluators (see `worker/registry.py`).

Per `services/evaluator/app/relevance.py`'s own docstring, turning a raw
span's flattened `input`/`output` into evaluatable text is an adapter
concern for whoever calls an evaluator -- this module, not
`services/evaluator`.
"""

from __future__ import annotations

from typing import Any

from app.relevance import RelevanceEvaluatorInput


def span_to_relevance_input(span: dict[str, Any]) -> RelevanceEvaluatorInput:
    """`span["input"]`/`span["output"]` are already flattened to `str | None`
    by the ingestion path (a plain string is stored verbatim; any other
    JSON-serializable value is stored as its compact `json.dumps(...)` text)
    -- see `apps/api/app/services/ingestion.py`'s `_normalize_text`. There is
    no nested JSON structure left to interpret here.

    `None` (the span had no `input`/`output` at all) becomes `""`, which
    `RelevanceEvaluatorInput.__post_init__` already accepts as a valid `str`
    and which `evaluate()` already routes to its well-defined `not_evaluable`
    outcome -- not an adapter-level error.

    `input_truncated`/`output_truncated` are deliberately not consulted here
    -- this evaluator has no way to represent "computed against truncated
    input" in its result today (a known, accepted gap, not an oversight of
    this adapter).
    """
    return RelevanceEvaluatorInput(
        input_text=span["input"] or "",
        output_text=span["output"] or "",
    )
