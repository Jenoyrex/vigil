"""Request/response models for the read-side Trace Explorer API
(`GET /v1/traces`, `GET /v1/traces/{trace_id}`,
`GET /v1/traces/{trace_id}/spans/{span_id}`).

`trace_id`/`span_id` validation intentionally duplicates the regex already
in app/schemas/traces.py (the ingestion request schema) rather than
importing its private helpers -- these are two independently-versioned
request/response surfaces (write vs. read), and ADR 001 explicitly accepts
this kind of small duplication over coupling unrelated modules together.

As with ingestion, `project_id` has no field anywhere in this module: it is
never accepted from the client on any read endpoint either. Every query
parameter here is either a filter or a pagination/format detail --
`project_id` always comes from the authenticated API key (app/api/deps.py)
and is threaded through app/services/query.py and
app/clickhouse/query_repository.py, never from request data.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel

TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")

TraceStatus = Literal["ok", "error", "unknown"]
SpanStatus = Literal["unset", "ok", "error"]


def _validate_trace_id(value: str) -> str:
    if not TRACE_ID_RE.fullmatch(value):
        raise ValueError("trace_id must be exactly 32 hexadecimal characters")
    return value.lower()


def _validate_span_id(value: str) -> str:
    if not SPAN_ID_RE.fullmatch(value):
        raise ValueError("span_id must be exactly 16 hexadecimal characters")
    return value.lower()


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("must be a timezone-aware RFC3339 timestamp (include a UTC offset)")
    return value


TraceId = Annotated[str, AfterValidator(_validate_trace_id)]
SpanId = Annotated[str, AfterValidator(_validate_span_id)]
AwareDatetime = Annotated[datetime, AfterValidator(_require_timezone_aware)]


class TraceSummary(BaseModel):
    """One row of `GET /v1/traces`. See app/services/query.py for how
    `status` is derived from a trace's spans
    (docs/decisions/002-trace-span-telemetry-model.md decision 6)."""

    trace_id: str
    start_time: datetime
    end_time: datetime
    duration_ms: int
    status: TraceStatus
    span_count: int
    error_span_count: int
    root_span_name: str | None
    environment: str
    resource: str


class TraceListResponse(BaseModel):
    """Ordered `start_time DESC, trace_id DESC` -- `trace_id` is the
    deterministic tie-breaker so keyset pagination (`next_cursor`) is
    stable even when multiple traces share the same `start_time`."""

    traces: list[TraceSummary]
    next_cursor: str | None = None


class EventOut(BaseModel):
    time: datetime
    name: str
    attributes: dict[str, str]


class SpanOut(BaseModel):
    """One span as returned by trace-detail or span-detail. Field set
    mirrors the `spans` ClickHouse table
    (docs/decisions/003-clickhouse-telemetry-storage.md section 2) minus
    `project_id` (never exposed -- implicit from auth) and `ingested_at`
    (an internal receipt timestamp, not client-facing)."""

    span_id: str
    parent_span_id: str | None
    name: str
    span_type: str
    resource: str
    start_time: datetime
    end_time: datetime
    duration_ms: int
    status: SpanStatus
    status_message: str | None
    input: str | None
    input_size_bytes: int
    input_truncated: bool
    output: str | None
    output_size_bytes: int
    output_truncated: bool
    attributes: dict[str, str]
    attributes_truncated: bool
    events: list[EventOut]
    events_truncated: bool
    llm_provider: str | None
    llm_model: str | None
    llm_input_tokens: int | None
    llm_output_tokens: int | None
    llm_total_tokens: int | None
    llm_cost_usd: str | None
    environment: str
    release: str | None


class TraceDetailResponse(BaseModel):
    """`spans` is ordered `start_time ASC`. This does NOT imply
    parent-before-child -- ADR 002 explicitly allows spans to arrive out of
    order, so the client must reconstruct the tree via `parent_span_id`,
    never by array position. `truncated`/`total_span_count` describe spans
    beyond the configured `max_spans_per_trace_response` cap."""

    trace_id: str
    status: TraceStatus
    start_time: datetime
    end_time: datetime
    duration_ms: int
    span_count: int
    total_span_count: int
    truncated: bool
    spans: list[SpanOut]
