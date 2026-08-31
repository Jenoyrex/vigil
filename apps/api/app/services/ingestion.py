"""Transforms a validated `TracesRequest` into ClickHouse `spans` row dicts.

Idempotency note (see docs/decisions/003-clickhouse-telemetry-storage.md):
this module and the repository it feeds are designed so that RETRYING an
identical request is safe -- ClickHouse's `ReplacingMergeTree` on `spans`
will eventually collapse duplicate `(project_id, trace_id, span_id)` rows
during background merges. This is NOT exactly-once delivery: a read
immediately after a retry can transiently see duplicate rows, and nothing in
this API path waits for or forces a merge. Callers needing immediate
per-span correctness must query with `FINAL` (or `LIMIT 1 BY`), same as any
other ClickHouse reader. The API never claims stronger guarantees than that.

Payload-limit precedence, per ADR 003 decision 10:
1. `input` and `output` are truncated independently, each to its own 64 KiB
   cap (UTF-8 bytes, cut on a valid character boundary).
2. Whatever remains of the 256 KiB total span budget after that is available
   to `attributes`, then `events` -- entries are kept in the client-supplied
   order until the next one would not fit, then the rest of that field are
   dropped (not silently: `attributes_truncated`/`events_truncated` record
   it). Nothing here tries to fit a "best" subset out of order; it is a
   simple, deterministic prefix-keep.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from app.config import settings
from app.schemas.traces import AttributeValue, ResourceModel, SpanIn, TracesRequest

EventRow = tuple[Any, str, dict[str, str]]


def transform_request(payload: TracesRequest, *, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """Build one ClickHouse row dict per span in `payload`.

    `project_id` must come from the authenticated API key (see
    app/api/deps.py) -- never from the request body.
    """
    resource_column_value = payload.resource.service_name or ""
    resource_attributes = _resource_extra_attributes(payload.resource)

    return [
        _transform_span(
            span,
            project_id=project_id,
            resource=resource_column_value,
            resource_attributes=resource_attributes,
        )
        for span in payload.spans
    ]


def _transform_span(
    span: SpanIn,
    *,
    project_id: uuid.UUID,
    resource: str,
    resource_attributes: dict[str, str],
) -> dict[str, Any]:
    input_stored, input_size, input_truncated = _truncate_field(
        span.input, settings.max_input_bytes
    )
    output_stored, output_size, output_truncated = _truncate_field(
        span.output, settings.max_output_bytes
    )

    span_attributes = _coerce_attributes(span.attributes)
    merged_attributes = {**resource_attributes, **span_attributes}

    event_rows = [
        (event.time, event.name, _coerce_attributes(event.attributes))
        for event in (span.events or [])
    ]

    remaining_budget = max(
        0, settings.max_total_span_bytes - _byte_len(input_stored) - _byte_len(output_stored)
    )
    kept_attributes, attributes_truncated, budget_after_attrs = _fit_attributes(
        merged_attributes, remaining_budget
    )
    kept_events, events_truncated = _fit_events(event_rows, budget_after_attrs)

    return {
        "project_id": project_id,
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "name": span.name,
        "span_type": span.span_type,
        "resource": resource,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "status": span.status,
        "status_message": span.status_message,
        "input": input_stored,
        "input_size_bytes": input_size,
        "input_truncated": input_truncated,
        "output": output_stored,
        "output_size_bytes": output_size,
        "output_truncated": output_truncated,
        "attributes": kept_attributes,
        "attributes_truncated": attributes_truncated,
        "events.time": [event[0] for event in kept_events],
        "events.name": [event[1] for event in kept_events],
        "events.attributes": [event[2] for event in kept_events],
        "events_truncated": events_truncated,
        "llm_provider": span.llm_provider,
        "llm_model": span.llm_model,
        "llm_input_tokens": span.llm_input_tokens,
        "llm_output_tokens": span.llm_output_tokens,
        "llm_total_tokens": span.llm_total_tokens,
        "llm_cost_usd": Decimal(str(span.llm_cost_usd)) if span.llm_cost_usd is not None else None,
        "environment": span.environment or "unknown",
        "release": span.release,
    }


def _resource_extra_attributes(resource: ResourceModel) -> dict[str, str]:
    """Resource fields not promoted to a span column, namespaced under `resource.*`."""
    attributes: dict[str, str] = {}
    if resource.sdk_name:
        attributes["resource.sdk.name"] = resource.sdk_name
    if resource.sdk_version:
        attributes["resource.sdk.version"] = resource.sdk_version
    for key, value in (resource.model_extra or {}).items():
        attributes[f"resource.{key}"] = _coerce_attr_value(value)
    return attributes


def _coerce_attributes(attributes: dict[str, AttributeValue] | None) -> dict[str, str]:
    if not attributes:
        return {}
    return {key: _coerce_attr_value(value) for key, value in attributes.items()}


def _coerce_attr_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _byte_len(value: str | None) -> int:
    return len(value.encode("utf-8")) if value else 0


def _truncate_field(value: Any, max_bytes: int) -> tuple[str | None, int, bool]:
    """Normalize `value` to text, then truncate to `max_bytes` UTF-8 bytes.

    Returns (stored_value, original_size_bytes, was_truncated). Truncation
    cuts on a UTF-8 character boundary (never splits a multi-byte
    codepoint) and always preserves the pre-truncation byte size, per ADR 003.
    """
    text = _normalize_text(value)
    if text is None:
        return None, 0, False

    encoded = text.encode("utf-8")
    original_size = len(encoded)
    if original_size <= max_bytes:
        return text, original_size, False

    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, original_size, True


def _fit_attributes(
    attributes: dict[str, str], budget_bytes: int
) -> tuple[dict[str, str], bool, int]:
    """Keep attribute entries, in order, while they fit `budget_bytes`.

    Returns (kept, was_truncated, remaining_budget_for_events).
    """
    kept: dict[str, str] = {}
    used = 0
    truncated = False
    for key, value in attributes.items():
        entry_size = len(key.encode("utf-8")) + len(value.encode("utf-8"))
        if used + entry_size > budget_bytes:
            truncated = True
            break
        kept[key] = value
        used += entry_size
    return kept, truncated, budget_bytes - used


def _event_size(event: EventRow) -> int:
    _, name, attributes = event
    size = len(name.encode("utf-8")) + 8  # +8: rough fixed cost of the timestamp
    for key, value in attributes.items():
        size += len(key.encode("utf-8")) + len(value.encode("utf-8"))
    return size


def _fit_events(events: list[EventRow], budget_bytes: int) -> tuple[list[EventRow], bool]:
    kept: list[EventRow] = []
    used = 0
    truncated = False
    for event in events:
        entry_size = _event_size(event)
        if used + entry_size > budget_bytes:
            truncated = True
            break
        kept.append(event)
        used += entry_size
    return kept, truncated
