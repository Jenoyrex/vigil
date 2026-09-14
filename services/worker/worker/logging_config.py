"""Structured (JSON Lines) logging configuration for services/worker --
Phase 4D, F4. Replaces the previous `logging.basicConfig(level=
logging.INFO)` plain-text setup (in both `worker/__main__.py` and
`worker/poller_main.py`) with one JSON object per line on stdout, so a
production log aggregator (or a human piping through `jq`) can filter on
fields instead of scraping message text. Stdlib `logging` only -- no new
dependency, matching apps/api/app/logging_config.py's identical choice
(duplicated rather than imported across the package boundary, per ADR 001
decision 6 -- these are two independently deployable services with no
shared library between them besides `vigil-evaluator`).

Local development sees the exact same JSON Lines output production does --
deliberately not a second, prettier formatter, so there is only ever one
code path to verify. Pipe through `jq` for a readable view locally; see
services/worker/README.md's "Logging" section for that tradeoff.

Fields on every record: `timestamp` (UTC, ISO 8601, millisecond precision),
`level`, `service` (fixed per process -- "worker" or "poller", since both
entrypoints share one image/package but are meaningfully different
processes operationally -- see services/worker/Dockerfile), `logger` (the
dotted module name that emitted it), and `message`. Anything passed via a
call's `extra={...}` argument (see worker/runtime.py, worker/poller.py, and
their neighbors) is merged in as additional top-level fields -- `worker_id`,
`job_id`, `project_id`, `evaluator_name`, etc. -- never nested, so a log
query can filter on them directly. `exception` carries a formatted
traceback when the record was logged with exception info attached
(`logger.exception(...)` or `logger.error(..., exc_info=True)`).

Deliberately NOT a contextvar-based auto-injection of `worker_id` the way
apps/api/app/logging_config.py auto-injects `request_id`: `worker.dispatcher
.Dispatcher.dispatch` runs job execution on a plain
`concurrent.futures.ThreadPoolExecutor`, which does not propagate a
context variable set on the submitting thread into the pool's worker
threads (unlike Starlette's sync-route thread offload, which explicitly
copies context) -- a contextvar here would silently stop covering exactly
the concurrent job-execution logs it would be most useful for. Passing
`worker_id`/`job_id`/etc. explicitly via each call's own `extra={...}` is
more verbose but correct regardless of which thread emits the record.

SECURITY: this formatter has no redaction logic of its own -- call sites
are the enforcement point. Never pass the internal service token, a
database URL, or raw span/evaluation input/output content via `extra`;
only identifiers (worker/job/project/trace/span ids, evaluator name/
version, counts, error type names) and other non-sensitive operational
metadata.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# The stdlib logging.LogRecord attributes every record has regardless of
# what was logged -- anything else found on the record came from a call's
# `extra={...}` and is a genuine structured field to surface. Kept
# identical to apps/api/app/logging_config.py's own list (from the logging
# module's own documented LogRecord attributes) rather than derived by
# diffing against a throwaway record, so this can't silently miss whatever
# a future Python version adds.
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

        # Only attributes that aren't part of every LogRecord came from this
        # call's own `extra={...}` -- sorted for a deterministic field
        # order, not because order carries any meaning. `default=str` and
        # the explicit reserved-attrs/exception handling here are what keep
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
    (already set by services/worker/Dockerfile) is what makes stdout safe
    as a production log sink: Docker's default `json-file` log driver
    captures a container's stdout/stderr directly, and an unbuffered stream
    is what guarantees a line is actually flushed promptly rather than
    sitting in a libc buffer.

    Idempotent -- clears any handlers a previous call installed on the root
    logger first -- though each entrypoint (`worker/__main__.py`,
    `worker/poller_main.py`) calls this exactly once, at import time, same
    as the `logging.basicConfig` call it replaces.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service))
    root.addHandler(handler)
    root.setLevel(level)
