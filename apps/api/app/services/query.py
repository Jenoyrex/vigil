"""Business logic for the read-side Trace Explorer API: time-window
validation, cursor encoding, and ClickHouse row -> response transformation.
Routes (app/api/v1/traces.py) call into this module; this module owns no
SQL itself -- see app/clickhouse/query_repository.py for that.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.clickhouse.query_repository import TracesQueryRepository
from app.schemas.query import (
    TRACE_ID_RE,
    EventOut,
    SpanOut,
    TraceDetailResponse,
    TraceListResponse,
    TraceSummary,
)


class QueryValidationError(ValueError):
    """A read-API request failed business-level validation (-> HTTP 422)."""


@dataclass(frozen=True)
class TimeWindow:
    start_time_from: datetime
    start_time_to: datetime


def _as_utc(value: datetime) -> datetime:
    """ClickHouse returns DateTime64 values as naive Python datetimes (no
    tzinfo attached) -- verified against clickhouse_connect 1.7.2, both for
    a raw column and an aggregate like min()/max(). The column itself has
    no timezone metadata, but every DateTime64 value in `spans` is UTC (the
    ingestion API and SDK both require timezone-aware input, normalized
    before storage) -- so attach UTC explicitly here rather than emitting
    an ambiguous, offset-less timestamp in an API response.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def resolve_time_window(
    start_time_from: datetime | None,
    start_time_to: datetime | None,
    *,
    default_window_hours: int,
    max_window_days: int,
) -> TimeWindow:
    """Default and bound a `[start_time_from, start_time_to)` window.

    Omitted bounds default relative to "now" (the previous
    `default_window_hours`). Always enforces `start_time_from <=
    start_time_to` and a maximum span of `max_window_days`, so a list or
    analytics query can never accidentally scan the full retention window.
    """
    now = datetime.now(UTC)
    resolved_to = start_time_to or now
    resolved_from = start_time_from or (resolved_to - timedelta(hours=default_window_hours))

    if resolved_from > resolved_to:
        raise QueryValidationError("start_time_from must not be after start_time_to.")
    if resolved_to - resolved_from > timedelta(days=max_window_days):
        raise QueryValidationError(
            f"Time window must not exceed {max_window_days} days; "
            f"narrow start_time_from/start_time_to."
        )
    return TimeWindow(resolved_from, resolved_to)


def encode_trace_cursor(start_time: datetime, trace_id: str) -> str:
    """Opaque pagination cursor for `GET /v1/traces`, encoding the previous
    page's last `(start_time, trace_id)` -- the same tuple `list_traces`
    sorts and filters by (`start_time DESC, trace_id DESC`). A forged/
    corrupted cursor can only produce `QueryValidationError` (422) or a
    wrong-but-still-project-scoped page -- the project_id WHERE clause is
    independent of cursor content -- so this does not need to be signed.
    """
    payload = json.dumps(
        {"start_time": start_time.isoformat(), "trace_id": trace_id}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_trace_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(payload)
        start_time = datetime.fromisoformat(data["start_time"])
        trace_id = str(data["trace_id"])
    except Exception as exc:
        raise QueryValidationError("Malformed pagination cursor.") from exc
    if start_time.tzinfo is None or not TRACE_ID_RE.fullmatch(trace_id):
        raise QueryValidationError("Malformed pagination cursor.")
    return start_time, trace_id.lower()


def _derive_status(error_count: int, root_count: int) -> str:
    """Trace status per docs/decisions/002-trace-span-telemetry-model.md
    decision 6: error if any span errored; else ok once a root span has
    arrived; else unknown."""
    if error_count > 0:
        return "error"
    if root_count > 0:
        return "ok"
    return "unknown"


def _duration_ms(start_time: datetime, end_time: datetime) -> int:
    return round((end_time - start_time).total_seconds() * 1000)


def _build_trace_summary(row: dict[str, Any]) -> TraceSummary:
    start_time = _as_utc(row["trace_start_time"])
    end_time = _as_utc(row["trace_end_time"])
    return TraceSummary(
        trace_id=row["trace_id"],
        start_time=start_time,
        end_time=end_time,
        duration_ms=_duration_ms(start_time, end_time),
        status=_derive_status(row["error_span_count"], row["root_span_count"]),
        span_count=row["span_count"],
        error_span_count=row["error_span_count"],
        root_span_name=row["root_span_name"] or None,
        environment=row["environment"],
        resource=row["resource"],
    )


def list_traces_response(
    repository: TracesQueryRepository,
    *,
    project_id: UUID,
    start_time_from: datetime | None,
    start_time_to: datetime | None,
    environment: str | None,
    resource: str | None,
    has_error: bool | None,
    limit: int,
    cursor: str | None,
    default_window_hours: int,
    max_window_days: int,
) -> TraceListResponse:
    window = resolve_time_window(
        start_time_from,
        start_time_to,
        default_window_hours=default_window_hours,
        max_window_days=max_window_days,
    )
    decoded_cursor = decode_trace_cursor(cursor) if cursor else None

    # Fetch one extra row so we can tell whether there's a next page
    # without a second round trip or an imprecise "same size as limit"
    # guess.
    rows = repository.list_traces(
        project_id=project_id,
        start_time_from=window.start_time_from,
        start_time_to=window.start_time_to,
        environment=environment,
        resource=resource,
        has_error=has_error,
        limit=limit + 1,
        cursor=decoded_cursor,
    )

    has_more = len(rows) > limit
    summaries = [_build_trace_summary(row) for row in rows[:limit]]

    next_cursor = None
    if has_more and summaries:
        last = summaries[-1]
        next_cursor = encode_trace_cursor(last.start_time, last.trace_id)

    return TraceListResponse(traces=summaries, next_cursor=next_cursor)


def _decimal_to_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _build_span_out(row: dict[str, Any]) -> SpanOut:
    events = [
        EventOut(time=_as_utc(event_time), name=event_name, attributes=event_attributes)
        for event_time, event_name, event_attributes in zip(
            row["events.time"], row["events.name"], row["events.attributes"], strict=True
        )
    ]
    return SpanOut(
        span_id=row["span_id"],
        parent_span_id=row["parent_span_id"],
        name=row["name"],
        span_type=row["span_type"],
        resource=row["resource"],
        start_time=_as_utc(row["start_time"]),
        end_time=_as_utc(row["end_time"]),
        duration_ms=row["duration_ms"],
        status=row["status"],
        status_message=row["status_message"],
        input=row["input"],
        input_size_bytes=row["input_size_bytes"],
        input_truncated=row["input_truncated"],
        output=row["output"],
        output_size_bytes=row["output_size_bytes"],
        output_truncated=row["output_truncated"],
        attributes=row["attributes"],
        attributes_truncated=row["attributes_truncated"],
        events=events,
        events_truncated=row["events_truncated"],
        llm_provider=row["llm_provider"],
        llm_model=row["llm_model"],
        llm_input_tokens=row["llm_input_tokens"],
        llm_output_tokens=row["llm_output_tokens"],
        llm_total_tokens=row["llm_total_tokens"],
        llm_cost_usd=_decimal_to_str(row["llm_cost_usd"]),
        environment=row["environment"],
        release=row["release"],
    )


def get_trace_response(
    repository: TracesQueryRepository,
    *,
    project_id: UUID,
    trace_id: str,
    start_date: date | None,
    max_spans: int,
) -> TraceDetailResponse | None:
    """`None` means "no spans found for this project_id/trace_id" -- the
    route maps that to 404.

    Two queries: an untruncated summary aggregate (status/counts/
    time-range, correct even if the trace has more spans than `max_spans`)
    and a separate, capped `FINAL` fetch of the spans actually returned --
    see `TracesQueryRepository.summarize_trace`'s docstring for why these
    must not be derived from the same, possibly-truncated, row set. Running
    the cheap summary query first also means the expensive `FINAL` query
    never runs at all for a trace_id that doesn't exist.
    """
    summary = repository.summarize_trace(
        project_id=project_id, trace_id=trace_id, start_date=start_date
    )
    if summary is None:
        return None

    rows = repository.get_trace_spans(
        project_id=project_id, trace_id=trace_id, start_date=start_date, limit=max_spans + 1
    )
    truncated = len(rows) > max_spans
    spans = [_build_span_out(row) for row in rows[:max_spans]]

    start_time = _as_utc(summary["trace_start_time"])
    end_time = _as_utc(summary["trace_end_time"])

    return TraceDetailResponse(
        trace_id=trace_id,
        status=_derive_status(summary["error_span_count"], summary["root_span_count"]),
        start_time=start_time,
        end_time=end_time,
        duration_ms=_duration_ms(start_time, end_time),
        span_count=len(spans),
        total_span_count=summary["total_span_count"],
        truncated=truncated,
        spans=spans,
    )


def get_span_response(
    repository: TracesQueryRepository,
    *,
    project_id: UUID,
    trace_id: str,
    span_id: str,
    start_date: date | None,
) -> SpanOut | None:
    rows = repository.get_span(
        project_id=project_id, trace_id=trace_id, span_id=span_id, start_date=start_date
    )
    if not rows:
        return None
    return _build_span_out(rows[0])
