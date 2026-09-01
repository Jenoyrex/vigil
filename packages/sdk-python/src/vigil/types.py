"""Shared type aliases and constants used across the Vigil SDK."""

from __future__ import annotations

from typing import Literal

SpanStatus = Literal["unset", "ok", "error"]
AttributeValue = str | int | float | bool

RECOMMENDED_SPAN_TYPES: frozenset[str] = frozenset(
    {"llm", "retrieval", "embedding", "tool", "function", "agent", "db", "http", "unknown"}
)
"""Recommended `span_type` vocabulary, per
docs/decisions/002-trace-span-telemetry-model.md. `span_type` is an open,
unconstrained string -- this constant is documentation/discoverability only
and is never enforced; arbitrary custom values are always accepted."""
