"""Shared request-building helpers for POST /v1/traces tests."""

from __future__ import annotations

from typing import Any

DEFAULT_TRACE_ID = "0" * 32
DEFAULT_SPAN_ID = "1" * 16


def valid_span(**overrides: Any) -> dict[str, Any]:
    span: dict[str, Any] = {
        "trace_id": DEFAULT_TRACE_ID,
        "span_id": DEFAULT_SPAN_ID,
        "parent_span_id": None,
        "name": "openai.chat.completion",
        "span_type": "llm",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:00:01Z",
        "status": "ok",
    }
    span.update(overrides)
    return span


def valid_traces_payload(
    spans: list[dict[str, Any]] | None = None, resource: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "resource": resource if resource is not None else {"service.name": "test-service"},
        "spans": spans if spans is not None else [valid_span()],
    }
