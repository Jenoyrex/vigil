"""Trace/span context propagation via `contextvars`.

`contextvars` (rather than a manual thread-local stack) is what makes nested
`with vigil.start_span(...)` blocks compose automatically: each span's
`__exit__` restores exactly the context that was active before its
`__enter__`, via the token returned by `ContextVar.set()`, and propagation
follows Python's own execution-context rules (correct across threads
started with context copies and across `asyncio` tasks), rather than a
hand-rolled stack this SDK would have to get right itself.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str


_current_span_context: contextvars.ContextVar[SpanContext | None] = contextvars.ContextVar(
    "vigil_current_span_context", default=None
)


def get_current_span_context() -> SpanContext | None:
    """The currently-active span's context, or `None` if there isn't one."""
    return _current_span_context.get()


def set_current_span_context(context: SpanContext) -> contextvars.Token:
    """Make `context` the active span context. Returns a token for `reset`."""
    return _current_span_context.set(context)


def reset_current_span_context(token: contextvars.Token) -> None:
    """Restore whatever span context was active before the matching `set`."""
    _current_span_context.reset(token)
