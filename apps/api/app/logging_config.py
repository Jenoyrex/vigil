"""Structured (JSON Lines) logging configuration for apps/api -- Phase 4D,
F4. Replaces the previous `logging.basicConfig(level=logging.INFO)` plain-
text setup with one JSON object per line on stdout, so a production log
aggregator (or a human piping through `jq`) can filter on fields instead of
scraping message text. Stdlib `logging` only -- no new dependency: this
repo's existing convention (every module already does
`logging.getLogger(__name__)`) needed a formatter and a call-site
convention, not a different logging framework, matching ADR 006's Phase 4D
resolution of its own "structured logging" deferred gap.

Local development sees the exact same JSON Lines output production does --
deliberately not a second, prettier formatter, so there is only ever one
code path to verify. Pipe through `jq` for a readable view locally; see
apps/api/README.md's "Logging" section for that tradeoff.

Fields on every record: `timestamp` (UTC, ISO 8601, millisecond precision),
`level`, `service` (fixed per process -- "api"), `logger` (the dotted
module name that emitted it), and `message`. Anything passed via a call's
`extra={...}` argument (see app/api/v1/traces.py and its neighbors) is
merged in as additional top-level fields -- `request_id`, `project_id`,
`trace_id`, `span_id`, etc. -- never nested, so a log query can filter on
them directly. `exception` carries a formatted traceback when the record
was logged with exception info attached (`logger.exception(...)` or
`logger.error(..., exc_info=True)`).

SECURITY: this formatter has no redaction logic of its own -- call sites
are the enforcement point. Never pass an API key, bearer token, the
internal service token, a database URL, or raw span input/output/
attributes via `extra`; only identifiers (request/project/trace/span ids,
counts, error type names) and other non-sensitive operational metadata.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

#: Set once per incoming HTTP request by `app.middleware.RequestIdMiddleware`
#: and read back by `JsonFormatter.format` below, so every log record
#: emitted while handling that request -- no matter how deep the call stack,
#: including app/clickhouse/query_common.py's shared query helper, which has
#: no request-scoped parameter of its own to thread one through -- carries
#: the same `request_id` automatically, without every intermediate function
#: needing to accept and forward one. Safe across FastAPI's sync route
#: handlers specifically because Starlette runs those via anyio's thread
#: offload, which copies the calling context (`contextvars.copy_context()`)
#: into the worker thread -- unlike a bare
#: `concurrent.futures.ThreadPoolExecutor.submit`, which would not.
_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vigil_api_request_id", default=None
)


def bind_request_id(request_id: str) -> contextvars.Token[str | None]:
    """Called once per request by `RequestIdMiddleware`. Returns a token
    `reset_request_id` must be given back at the end of that same request --
    never reused across requests.
    """
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token[str | None]) -> None:
    _request_id_var.reset(token)


def get_request_id() -> str:
    """The current request's id, or a freshly generated one if called
    outside `RequestIdMiddleware` (defensive only -- every real request
    goes through that middleware; this just keeps a direct/unit-test call
    from ever observing `None` where, e.g.,
    `TracesIngestResponse.request_id: str` requires a value).
    """
    return _request_id_var.get() or str(uuid.uuid4())


# The stdlib logging.LogRecord attributes every record has regardless of
# what was logged -- anything else found on the record came from a call's
# `extra={...}` and is a genuine structured field to surface. Listed
# explicitly (from the logging module's own documented LogRecord
# attributes) rather than derived by diffing against a throwaway record, so
# this can't silently miss whatever a future Python version adds.
_RESERVED_LOG_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    }
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line. `service` is fixed at construction (one
    formatter instance per process, installed once by `configure_logging`);
    every other field is derived from the `LogRecord` itself, so no call
    site needs to know this formatter exists.
    """

    def __init__(self, *, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = _request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id

        # Only attributes that aren't part of every LogRecord came from this
        # call's own `extra={...}` -- sorted for a deterministic field order,
        # not because order carries any meaning. `default=str` and the
        # explicit reserved-attrs/exception handling above are what keep
        # this a single json.dumps call -- never nested JSON-encoding of an
        # already-serialized value.
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_LOG_RECORD_ATTRS
        }
        for key in sorted(extra):
            payload.setdefault(key, extra[key])

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(*, service: str, level: str) -> None:
    """Replaces `logging.basicConfig(level=logging.INFO)`. Installs exactly
    one `StreamHandler(sys.stdout)` carrying a `JsonFormatter` on the root
    logger -- every module's existing `logging.getLogger(__name__)` call
    inherits it unchanged, no per-module wiring needed. `PYTHONUNBUFFERED=1`
    (already set by apps/api/Dockerfile) is what makes stdout safe as a
    production log sink: Docker's default `json-file` log driver captures a
    container's stdout/stderr directly, and an unbuffered stream is what
    guarantees a line is actually flushed promptly rather than sitting in a
    libc buffer.

    Idempotent -- clears any handlers a previous call installed on the root
    logger first -- though `app/main.py` calls this exactly once, at import
    time, same as the `logging.basicConfig` call it replaces.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service))
    root.addHandler(handler)
    root.setLevel(level)
