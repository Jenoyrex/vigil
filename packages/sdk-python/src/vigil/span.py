"""A single span: the unit of telemetry sent to Vigil.

Created via `Vigil.start_span(...)` and used as a context manager. Per
docs/decisions/002-trace-span-telemetry-model.md, V1 only ever sends
*complete* spans -- there is no incremental start/end update to a span
already sent -- so a span is only handed to the client for delivery when
its `with` block exits, not when it is created.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from vigil.context import SpanContext, reset_current_span_context, set_current_span_context
from vigil.types import AttributeValue, SpanStatus

if TYPE_CHECKING:
    import contextvars

    from vigil.client import Vigil

_VALID_STATUSES: frozenset[str] = frozenset({"unset", "ok", "error"})


class Span:
    """One span. Do not construct directly -- use `Vigil.start_span(...)`."""

    def __init__(
        self,
        *,
        client: Vigil,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        name: str,
        span_type: str,
    ) -> None:
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.span_type = span_type

        self.status: SpanStatus = "unset"
        self.status_message: str | None = None
        self.input: Any = None
        self.output: Any = None
        self.attributes: dict[str, AttributeValue] = {}

        self.llm_provider: str | None = None
        self.llm_model: str | None = None
        self.llm_input_tokens: int | None = None
        self.llm_output_tokens: int | None = None
        self.llm_total_tokens: int | None = None
        self.llm_cost_usd: float | None = None

        self.start_time: datetime | None = None
        self.end_time: datetime | None = None

        self._client = client
        self._context_token: contextvars.Token | None = None

    # -- span API ------------------------------------------------------

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        """Set one attribute. `value` must be a str, int, float, or bool --
        the same primitive types the ingestion API's `attributes` map
        accepts (see docs/decisions/002-trace-span-telemetry-model.md)."""
        if not isinstance(value, str | int | float | bool):
            raise TypeError(
                f"Attribute {key!r} must be a str, int, float, or bool "
                f"(got {type(value).__name__})."
            )
        self.attributes[key] = value

    def set_status(self, status: SpanStatus, status_message: str | None = None) -> None:
        """Set the span's status. `status` must be `unset`, `ok`, or `error`,
        matching OpenTelemetry's three-value status model."""
        if status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}.")
        self.status = status
        self.status_message = status_message

    def set_input(self, value: Any) -> None:
        """Set the span's input. Any JSON-compatible value (or `None`) --
        preserved as-is; the server enforces the authoritative 64 KiB
        truncation limit, this SDK does not pre-truncate."""
        self.input = value

    def set_output(self, value: Any) -> None:
        """Set the span's output. Same handling as `set_input`."""
        self.output = value

    def record_llm_usage(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Record LLM usage/cost fields (the ingestion API's fast-path LLM
        columns). Only the fields explicitly passed are updated, so this can
        be called more than once as more information becomes available --
        e.g. `record_llm_usage(provider="openai")` up front, then token
        counts once a (possibly streamed) response finishes."""
        if provider is not None:
            self.llm_provider = provider
        if model is not None:
            self.llm_model = model
        if input_tokens is not None:
            self.llm_input_tokens = input_tokens
        if output_tokens is not None:
            self.llm_output_tokens = output_tokens
        if total_tokens is not None:
            self.llm_total_tokens = total_tokens
        if cost_usd is not None:
            self.llm_cost_usd = cost_usd

    # -- context manager -------------------------------------------------

    def __enter__(self) -> Span:
        self.start_time = datetime.now(UTC)
        self._context_token = set_current_span_context(
            SpanContext(trace_id=self.trace_id, span_id=self.span_id)
        )
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any
    ) -> bool:
        if exc is not None and self.status == "unset":
            self.status = "error"
            self.status_message = f"{exc_type.__name__}: {exc}" if exc_type else None
        self.end_time = datetime.now(UTC)
        if self._context_token is not None:
            reset_current_span_context(self._context_token)
        self._client._enqueue(self)
        return False  # never suppress the caller's exception
