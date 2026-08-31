"""Request/response models for `POST /v1/traces`.

Shape follows the ingestion envelope approved in
docs/decisions/002-trace-span-telemetry-model.md: one shared `resource`
object plus a `spans` array that may contain spans from multiple traces, or
an incomplete trace -- the API accepts spans independently and never
requires a complete trace to be present in one request.

`project_id` deliberately has no field anywhere in this module: it is never
accepted from the client. If a request body includes one, it is silently
ignored (extra="ignore") rather than rejected, matching the open/extensible
philosophy ADR 002 sets for spans generally -- but it is never read, and
authorization always uses the server-derived value from the authenticated
API key instead. See app/api/deps.py.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings

_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")

AttributeValue = str | int | float | bool
SpanStatus = Literal["unset", "ok", "error"]


def _validate_trace_id(value: str) -> str:
    if not _TRACE_ID_RE.fullmatch(value):
        raise ValueError("trace_id must be exactly 32 hexadecimal characters")
    return value.lower()


def _validate_span_id(value: str) -> str:
    if not _SPAN_ID_RE.fullmatch(value):
        raise ValueError("span_id must be exactly 16 hexadecimal characters")
    return value.lower()


class ResourceModel(BaseModel):
    """The batch-level `resource` object, mirroring OTel's `ResourceSpans`.

    Unrecognized keys are kept (via `extra="allow"`) and folded into every
    span's `attributes` map under a `resource.*` namespace at transform time,
    rather than being dropped -- see app/services/ingestion.py.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sdk_name: str | None = Field(default=None, alias="sdk.name")
    sdk_version: str | None = Field(default=None, alias="sdk.version")
    service_name: str | None = Field(
        default=None,
        alias="service.name",
        description="Denormalized onto every span's `resource` column.",
    )


class EventIn(BaseModel):
    """A single span event (e.g. an exception), per OTel's span event shape."""

    time: datetime
    name: str = Field(min_length=1)
    attributes: dict[str, AttributeValue] | None = None


class SpanIn(BaseModel):
    """One span. See docs/decisions/002-trace-span-telemetry-model.md decision 2."""

    model_config = ConfigDict(extra="ignore")

    trace_id: str = Field(description="32 hex characters (128-bit, OTel/W3C-compatible).")
    span_id: str = Field(description="16 hex characters (64-bit, OTel/W3C-compatible).")
    parent_span_id: str | None = Field(
        default=None, description="16 hex characters, or null for the root span."
    )

    name: str = Field(min_length=1)
    span_type: str = Field(
        default="unknown",
        min_length=1,
        description=(
            "Open, unconstrained string (e.g. `llm`, `retrieval`, `tool`). "
            "Not database-enforced -- unrecognized values are accepted."
        ),
    )

    start_time: datetime
    end_time: datetime

    status: SpanStatus = "unset"
    status_message: str | None = None

    input: Any | None = Field(
        default=None,
        description=(
            "String or JSON-serializable value. Truncated at 64 KiB "
            "(UTF-8 bytes); truncation is recorded, never silent."
        ),
    )
    output: Any | None = Field(
        default=None, description="Same handling as `input`, truncated at 64 KiB."
    )

    attributes: dict[str, AttributeValue] | None = None
    events: list[EventIn] | None = None

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_input_tokens: int | None = Field(default=None, ge=0)
    llm_output_tokens: int | None = Field(default=None, ge=0)
    llm_total_tokens: int | None = Field(default=None, ge=0)
    llm_cost_usd: float | None = Field(default=None, ge=0)

    environment: str | None = None
    release: str | None = None

    @model_validator(mode="after")
    def _validate_ids_and_times(self) -> SpanIn:
        self.trace_id = _validate_trace_id(self.trace_id)
        self.span_id = _validate_span_id(self.span_id)
        if self.parent_span_id is not None:
            self.parent_span_id = _validate_span_id(self.parent_span_id)

        name = self.name.strip()
        if not name:
            raise ValueError("name must not be empty")
        self.name = name

        if self.end_time < self.start_time:
            raise ValueError("end_time must be >= start_time")

        return self


class TracesRequest(BaseModel):
    """`POST /v1/traces` request body."""

    model_config = ConfigDict(extra="ignore")

    resource: ResourceModel = Field(default_factory=ResourceModel)
    spans: list[SpanIn] = Field(
        min_length=1,
        max_length=settings.max_spans_per_request,
        description="One or more spans. May span multiple traces or an incomplete trace.",
    )


class TracesIngestResponse(BaseModel):
    """`POST /v1/traces` response body.

    Deliberately minimal: the number of spans accepted for storage, plus a
    request id for correlating with server logs. This is NOT an idempotency
    or delivery receipt -- see app/services/ingestion.py's module docstring.
    """

    accepted: int
    request_id: str
