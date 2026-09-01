"""Wire-format serialization: `Span` -> the per-span JSON shape expected by
`POST /v1/traces` (see docs/decisions/002-trace-span-telemetry-model.md and
apps/api/app/schemas/traces.py's `SpanIn` -- this must stay in sync with
that schema).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from vigil.span import Span


def serialize_span(span: Span) -> dict[str, Any]:
    """Build the JSON-compatible dict for one span, called once a span's
    `with` block has exited (both `start_time` and `end_time` are set)."""
    return {
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "name": span.name,
        "span_type": span.span_type,
        "start_time": _isoformat(span.start_time),
        "end_time": _isoformat(span.end_time),
        "status": span.status,
        "status_message": span.status_message,
        "input": span.input,
        "output": span.output,
        "attributes": span.attributes,
        "llm_provider": span.llm_provider,
        "llm_model": span.llm_model,
        "llm_input_tokens": span.llm_input_tokens,
        "llm_output_tokens": span.llm_output_tokens,
        "llm_total_tokens": span.llm_total_tokens,
        "llm_cost_usd": span.llm_cost_usd,
    }


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
