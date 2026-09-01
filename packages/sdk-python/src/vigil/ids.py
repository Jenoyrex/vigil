"""OpenTelemetry/W3C-compatible trace and span ID generation.

Per docs/decisions/002-trace-span-telemetry-model.md decision 4: `trace_id`
is a 128-bit value, hex-encoded as 32 characters; `span_id` is a 64-bit
value, hex-encoded as 16 characters -- the same format used by the W3C
Trace Context header and OpenTelemetry SDKs generally.
"""

from __future__ import annotations

import secrets


def generate_trace_id() -> str:
    """A new 128-bit trace ID, as 32 lowercase hex characters."""
    return secrets.token_hex(16)


def generate_span_id() -> str:
    """A new 64-bit span ID, as 16 lowercase hex characters."""
    return secrets.token_hex(8)
