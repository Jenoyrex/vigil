"""The Vigil client: configuration, span creation, batching, and delivery.

Delivery model: completing a span (`with vigil.start_span(...)` exiting)
only ever appends to an in-memory buffer -- it never does network I/O, so
instrumented application code is never blocked on telemetry delivery. A
background daemon thread flushes that buffer periodically (`flush_interval`)
and whenever it reaches `max_batch_size`. `flush()` and `close()` instead
deliver synchronously in the calling thread/goroutine-equivalent, so their
success/failure is deterministic and, for `flush()`, raises on failure.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import threading
from typing import Any

import httpx

from vigil._version import __version__
from vigil.batching import BoundedBatchBuffer
from vigil.context import get_current_span_context
from vigil.exceptions import VigilConfigurationError, VigilDeliveryError, VigilFlushError
from vigil.ids import generate_span_id, generate_trace_id
from vigil.serialize import serialize_span
from vigil.span import Span
from vigil.transport import Transport

logger = logging.getLogger("vigil")

# Deliberately no DEFAULT_BASE_URL. Vigil has no public hosted ingestion
# endpoint yet, and a placeholder default (even one obviously fake, like
# `https://ingest.vigil.example.com`) risks being silently copy-pasted into
# a real deployment and mistaken for one. `base_url` is therefore required,
# exactly like `api_key` -- see the `VigilConfigurationError` below.

_ENV_API_KEY = "VIGIL_API_KEY"
_ENV_BASE_URL = "VIGIL_BASE_URL"

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_BATCH_SIZE = 100
_DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0
_DEFAULT_MAX_QUEUE_SIZE = 10_000
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE_SECONDS = 0.5
_DEFAULT_BACKOFF_MAX_SECONDS = 8.0
_SHUTDOWN_JOIN_TIMEOUT_SECONDS = 5.0


class Vigil:
    """The Vigil SDK client.

        vigil = Vigil(api_key="vgl_...", base_url="http://127.0.0.1:8000")
        with vigil.start_span("my operation", span_type="function") as span:
            span.set_attribute("key", "value")
        vigil.flush()
        vigil.close()

    Also usable as a context manager (`with Vigil(...) as vigil:`), which
    calls `close()` on exit.

    A background daemon thread periodically delivers buffered spans -- see
    the module docstring. Being a daemon thread, it will not by itself keep
    the process alive, and an `atexit` hook makes a best-effort attempt to
    flush on interpreter shutdown; neither is a substitute for calling
    `close()` explicitly when you control the shutdown path, since abrupt
    process termination (a crash, `os._exit`, `SIGKILL`) can skip both.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        service_name: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL_SECONDS,
        max_queue_size: int = _DEFAULT_MAX_QUEUE_SIZE,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        retry_backoff_base: float = _DEFAULT_BACKOFF_BASE_SECONDS,
        retry_backoff_max: float = _DEFAULT_BACKOFF_MAX_SECONDS,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Args:
            api_key: Vigil API key (`vgl_...`). Falls back to the
                `VIGIL_API_KEY` environment variable. Required one way or
                the other -- raises `VigilConfigurationError` if neither is
                set. Never logged.
            base_url: Ingestion API base URL. Falls back to the
                `VIGIL_BASE_URL` environment variable. Required one way or
                the other -- there is no default, since Vigil has no public
                hosted endpoint yet and a placeholder default risks being
                mistaken for one. Raises `VigilConfigurationError` if
                neither is set (e.g. `http://127.0.0.1:8000` for a local
                `apps/api`).
            service_name: Sent as `resource["service.name"]` on every batch.
                Optional -- the ingestion API does not require it -- but
                strongly recommended so telemetry from different services
                can be told apart.
            timeout: Per-request HTTP timeout, in seconds.
            max_batch_size: Flush automatically once this many spans are
                buffered.
            flush_interval: Also flush automatically at least this often
                (seconds), even if `max_batch_size` hasn't been reached.
            max_queue_size: Hard cap on buffered-but-undelivered spans.
                Once reached, new spans are dropped (with a logged warning)
                rather than growing the buffer without bound.
            max_retries: Additional delivery attempts after the first, for
                retryable failures (network errors, timeouts, 5xx/503).
            retry_backoff_base: Initial retry delay, in seconds; doubles
                each subsequent attempt up to `retry_backoff_max`.
            retry_backoff_max: Cap on the retry delay, in seconds.
            _transport: Internal/testing hook -- an `httpx.BaseTransport` to
                use instead of real network I/O (e.g. `httpx.MockTransport`
                in tests). Not part of the supported public API.
        """
        resolved_key = api_key or os.environ.get(_ENV_API_KEY)
        if not resolved_key:
            raise VigilConfigurationError(
                f"No Vigil API key provided. Pass api_key=... or set the "
                f"{_ENV_API_KEY} environment variable."
            )
        resolved_base_url = base_url or os.environ.get(_ENV_BASE_URL)
        if not resolved_base_url:
            raise VigilConfigurationError(
                f"No Vigil base_url provided. Vigil has no default/public "
                f"ingestion endpoint yet, so this must be set explicitly -- "
                f"pass base_url=... or set the {_ENV_BASE_URL} environment "
                f"variable (e.g. http://127.0.0.1:8000 for a local apps/api)."
            )
        if max_batch_size <= 0:
            raise VigilConfigurationError("max_batch_size must be a positive integer.")
        if max_queue_size <= 0:
            raise VigilConfigurationError("max_queue_size must be a positive integer.")
        if flush_interval <= 0:
            raise VigilConfigurationError("flush_interval must be a positive number of seconds.")
        if max_retries < 0:
            raise VigilConfigurationError("max_retries must not be negative.")

        self._resource: dict[str, Any] = {
            "sdk.name": "vigil-python",
            "sdk.version": __version__,
        }
        if service_name:
            self._resource["service.name"] = service_name

        self._http = httpx.Client(
            base_url=resolved_base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {resolved_key}"},
            transport=_transport,
        )
        self._transport = Transport(
            self._http,
            max_retries=max_retries,
            backoff_base_seconds=retry_backoff_base,
            backoff_max_seconds=retry_backoff_max,
        )
        self._buffer = BoundedBatchBuffer(
            max_batch_size=max_batch_size, max_queue_size=max_queue_size
        )
        self._flush_interval = flush_interval

        self._closed = False
        self._close_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop, name="vigil-flush", daemon=True
        )
        self._worker.start()
        atexit.register(self._atexit_close)

    # -- span creation -----------------------------------------------------

    def start_span(self, name: str, *, span_type: str = "unknown") -> Span:
        """Start a new span. Use as a context manager:

            with vigil.start_span("my operation", span_type="function") as span:
                ...

        If this call happens inside another currently-open
        `with vigil.start_span(...)` block, the new span automatically
        inherits that span's `trace_id` and becomes its child
        (`parent_span_id`), via `contextvars`. Otherwise it becomes the root
        of a brand-new trace (a fresh `trace_id`, `parent_span_id=None`).

        `span_type` is an open string -- the recommended vocabulary
        (`vigil.RECOMMENDED_SPAN_TYPES`) includes `llm`, `retrieval`,
        `embedding`, `tool`, `function`, `agent`, `db`, and `http`, but any
        custom value is accepted.
        """
        parent = get_current_span_context()
        if parent is None:
            trace_id = generate_trace_id()
            parent_span_id = None
        else:
            trace_id = parent.trace_id
            parent_span_id = parent.span_id

        return Span(
            client=self,
            trace_id=trace_id,
            span_id=generate_span_id(),
            parent_span_id=parent_span_id,
            name=name,
            span_type=span_type,
        )

    # -- internal: called by Span.__exit__ ----------------------------------

    def _enqueue(self, span: Span) -> None:
        if self._closed:
            logger.warning(
                "vigil: dropping span %s (%s) -- client already closed",
                span.span_id,
                span.name,
            )
            return

        row = serialize_span(span)
        added, should_flush = self._buffer.add(row)
        if not added:
            logger.warning(
                "vigil: telemetry buffer is full -- dropping span %s (%s)",
                span.span_id,
                span.name,
            )
            return
        if should_flush:
            self._wake_event.set()

    # -- flush / delivery ----------------------------------------------------

    def flush(self) -> None:
        """Synchronously deliver all currently-buffered spans.

        Blocks until delivery succeeds or retries are exhausted. Raises
        `VigilFlushError` if delivery ultimately fails -- unlike automatic
        background delivery, an explicit `flush()` call is expected to
        surface failures to the caller. Either way, the drained batch is not
        retained after this call returns (V1 does not persist or re-queue a
        batch that failed to send).
        """
        if self._closed:
            return
        batch = self._buffer.drain()
        if not batch:
            return
        try:
            self._transport.send_batch(self._resource, batch)
        except VigilDeliveryError as exc:
            raise VigilFlushError(
                f"Failed to deliver {len(batch)} span(s) to Vigil.",
                dropped_spans=len(batch),
            ) from exc

    def _background_flush(self) -> None:
        batch = self._buffer.drain()
        if not batch:
            return
        try:
            self._transport.send_batch(self._resource, batch)
        except VigilDeliveryError as exc:
            logger.warning(
                "vigil: background telemetry delivery failed; dropped %d span(s): %s",
                len(batch),
                exc,
            )

    def _worker_loop(self) -> None:
        while not self._shutdown_event.is_set():
            self._wake_event.wait(timeout=self._flush_interval)
            self._wake_event.clear()
            try:
                self._background_flush()
            except Exception:
                # Last line of defense: nothing above should raise anything
                # but VigilDeliveryError (already caught), but this daemon
                # thread must never die from an unexpected bug -- that would
                # silently stop all future telemetry delivery for the rest
                # of the process's life with no way for the caller to know.
                logger.exception("vigil: unexpected error in background flush loop")

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Flush remaining spans and shut down the background worker.

        Idempotent -- safe to call more than once, from any thread. After
        `close()`, new spans are dropped (with a logged warning) rather than
        raising, since instrumentation code should never fail just because
        telemetry shutdown already happened.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        self._shutdown_event.set()
        self._wake_event.set()
        self._worker.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_SECONDS)

        # Safety net for the narrow window between the worker's last drain
        # and this method observing it as joined -- BoundedBatchBuffer's
        # locking guarantees any such spans were never sent, never lost to
        # a race, and are drained here exactly once.
        batch = self._buffer.drain()
        if batch:
            try:
                self._transport.send_batch(self._resource, batch)
            except VigilDeliveryError as exc:
                logger.warning(
                    "vigil: dropped %d span(s) while closing: %s", len(batch), exc
                )

        self._http.close()

    def _atexit_close(self) -> None:
        # atexit hooks must never raise -- interpreter shutdown may have
        # already torn down modules (including logging) this depends on.
        with contextlib.suppress(Exception):
            self.close()

    def __enter__(self) -> Vigil:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
