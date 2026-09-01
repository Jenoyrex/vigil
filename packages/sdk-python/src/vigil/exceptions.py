"""Vigil SDK exception hierarchy.

Messages raised here must never include API key values or span `input`/
`output`/attribute content -- only counts, HTTP status codes, and generic
error-class names. (A span's own `status_message`, which can capture *user*
exception text when a `with vigil.start_span(...)` block raises, is a
different, expected mechanism -- see `vigil.span.Span.__exit__` -- not
covered by this restriction.)
"""

from __future__ import annotations


class VigilError(Exception):
    """Base class for all errors raised by the Vigil SDK."""


class VigilConfigurationError(VigilError):
    """Raised for invalid or missing SDK configuration (e.g. no API key)."""


class VigilDeliveryError(VigilError):
    """A batch could not be delivered to the Vigil API.

    Raised internally by the HTTP transport after retries are exhausted, or
    immediately for a non-retryable failure (e.g. 401/422). Not typically
    caught directly: `Vigil.flush()` wraps it in `VigilFlushError`, and
    automatic/background delivery catches and logs it instead of raising.
    """


class VigilFlushError(VigilError):
    """Raised by `Vigil.flush()` when delivery ultimately fails.

    Background/automatic delivery never raises this -- it only ever
    surfaces from an explicit, synchronous `flush()` call. The spans in the
    failed batch are dropped either way; V1 does not persist or re-queue a
    batch that failed to send.
    """

    def __init__(self, message: str, *, dropped_spans: int = 0) -> None:
        super().__init__(message)
        self.dropped_spans = dropped_spans
