# Vigil Python SDK

`packages/sdk-python` is the official Python SDK for sending trace/span telemetry to a Vigil
ingestion API (`POST /v1/traces`, see `apps/api`). It is an independent Python project managed
with [uv](https://github.com/astral-sh/uv); it does not share a workspace or dependency lockfile
with any other part of the monorepo, and it never imports from `apps/api` or any other internal
Vigil service -- it only ever talks to Vigil over HTTP, matching
`docs/decisions/001-system-architecture.md`.

## Installation

From within this directory:

```bash
uv sync
```

This creates a local `.venv` and installs both runtime and development dependencies. The only
runtime dependency is [`httpx`](https://www.python-httpx.org/).

## Quick start

```python
from vigil import Vigil

vigil = Vigil(api_key="vgl_...", base_url="http://127.0.0.1:8000")

with vigil.start_span("my operation", span_type="function") as span:
    span.set_attribute("key", "value")

vigil.flush()
vigil.close()
```

See `examples/python-sdk/basic.py` (repo root) for a runnable end-to-end example, including a
nested trace.

## Configuration

```python
vigil = Vigil(
    api_key=...,        # or the VIGIL_API_KEY environment variable
    base_url=...,        # or the VIGIL_BASE_URL environment variable
    service_name=...,    # optional but recommended -- see "Resource" below
    timeout=10.0,         # per-request HTTP timeout, in seconds
)
```

| Argument | Env var | Default | Notes |
|---|---|---|---|
| `api_key` | `VIGIL_API_KEY` | *(required)* | Raises `VigilConfigurationError` if neither is set. Never logged. |
| `base_url` | `VIGIL_BASE_URL` | *(required)* | Raises `VigilConfigurationError` if neither is set. See below. |
| `service_name` | -- | `None` | Sent as `resource["service.name"]`. |
| `timeout` | -- | `10.0` | HTTP request timeout, in seconds. |
| `max_batch_size` | -- | `100` | Flush automatically once this many spans are buffered. |
| `flush_interval` | -- | `5.0` | Also flush at least this often (seconds), even under `max_batch_size`. |
| `max_queue_size` | -- | `10000` | Hard cap on buffered spans; excess spans are dropped, not queued unboundedly. |
| `max_retries` | -- | `3` | Additional delivery attempts for transient failures. |
| `retry_backoff_base` / `retry_backoff_max` | -- | `0.5` / `8.0` | Exponential backoff bounds, in seconds. |

### API key

Vigil API keys look like `vgl_<prefix>.<secret>`. For local development against `apps/api`, mint
one with `uv run python scripts/seed_local_api_key.py` (from `apps/api`) -- see
`apps/api/README.md`. The key is never logged by this SDK, and delivery-failure error messages are
written to never include it.

### Base URL

There is **no default `base_url`** -- Vigil has no public hosted ingestion endpoint yet, and a
placeholder default would risk being copy-pasted into a real deployment and mistaken for one.
You must set `base_url=` or `VIGIL_BASE_URL` explicitly (e.g. `http://127.0.0.1:8000` for a local
`apps/api`); constructing `Vigil(...)` without one raises `VigilConfigurationError`.

## Nested spans

`Vigil.start_span(...)` returns a context manager. A span started inside another currently-open
`with vigil.start_span(...)` block automatically inherits that span's `trace_id` and becomes its
child (`parent_span_id`) -- there is no manual trace/span-id plumbing:

```python
with vigil.start_span("agent") as agent:
    with vigil.start_span("retrieval", span_type="retrieval"):
        ...

    with vigil.start_span("llm call", span_type="llm"):
        ...
```

A span started with no other span currently active becomes the root of a brand-new trace (a fresh
32-hex-character `trace_id`, `parent_span_id=None`). Propagation uses `contextvars`, so it follows
Python's own execution-context rules (correct across `asyncio` tasks and threads started with a
copied context).

IDs are OpenTelemetry/W3C-compatible: `trace_id` is 32 lowercase hex characters (128 bits),
`span_id` is 16 lowercase hex characters (64 bits).

Only complete spans are ever sent -- a span is buffered for delivery when its `with` block exits,
not when it starts. There is no incremental start/end update to an already-sent span in V1.

## Span API

```python
span.set_attribute(key, value)      # value: str | int | float | bool
span.set_status(status, status_message=None)   # status: "unset" | "ok" | "error"
span.set_input(value)               # any JSON-compatible value, or None
span.set_output(value)              # same
span.record_llm_usage(
    provider=None, model=None,
    input_tokens=None, output_tokens=None, total_tokens=None,
    cost_usd=None,
)
```

`record_llm_usage` only updates the fields you pass -- call it more than once as more information
becomes available (e.g. `provider`/`model` up front, token counts once a response finishes).

If an exception propagates out of a `with vigil.start_span(...)` block, the span's status is set to
`"error"` (with the exception recorded in `status_message`) unless you already called
`set_status(...)` yourself -- and the exception still propagates to your code. Vigil never
suppresses your application's exceptions.

### `span_type`

An open, unconstrained string -- any value is accepted. `vigil.RECOMMENDED_SPAN_TYPES` lists the
documented vocabulary for discoverability:

```
llm, retrieval, embedding, tool, function, agent, db, http, unknown
```

...but custom values are always fine (`span_type="my-custom-kind"`).

## Resource

Every batch includes a shared `resource` object:

```json
{ "sdk.name": "vigil-python", "sdk.version": "0.1.0", "service.name": "checkout-service" }
```

`sdk.name`/`sdk.version` are always sent automatically. `service.name` is only included if you pass
`service_name=...` -- the ingestion API does not require it, but it is strongly recommended so
telemetry from different services can be told apart.

## Batching and background delivery

Completing a span (`with` block exiting) only ever appends to an in-memory buffer -- it never does
network I/O, so your application is never blocked waiting for telemetry delivery. A background
**daemon thread** flushes that buffer:

- automatically, once `max_batch_size` spans have accumulated,
- automatically, at least every `flush_interval` seconds,
- when you call `vigil.flush()` explicitly (synchronously, in your own thread), or
- when you call `vigil.close()`.

The buffer is bounded by `max_queue_size`: once full, new spans are dropped (with a logged
warning) rather than growing memory usage without bound. There is no persistent/on-disk queue, no
Redis, and no separate worker process in V1 -- see "Limitations" below.

Being a daemon thread, the background worker will not by itself keep your process alive, and an
`atexit` hook makes a best-effort attempt to flush on normal interpreter shutdown. Neither is a
substitute for calling `vigil.close()` explicitly wherever you control application shutdown --
abrupt termination (a crash, `os._exit`, `SIGKILL`) can skip both.

## Retries

Only failures that could plausibly succeed on a retry are retried, with bounded exponential
backoff (`retry_backoff_base` doubling up to `retry_backoff_max`, for up to `max_retries`
additional attempts):

- network errors and timeouts,
- HTTP `503` and any other `5xx`.

These are **not** retried -- resending the identical request cannot fix them:

- `401` / `403` (bad or revoked API key),
- `422` (the payload itself is invalid),
- any other non-`5xx` response.

**A retry always resends the exact same spans, with the exact same `trace_id`/`span_id` values --
IDs are never regenerated on retry.** This is what makes retries safe: the ingestion API's
`(project_id, trace_id, span_id)` idempotency (see
`docs/decisions/003-clickhouse-telemetry-storage.md`) can only deduplicate a retried span if it is
actually presented as the same span.

## Failure behavior

This is the most important operational property of the SDK: **a telemetry delivery failure must
never break your application.**

- Completing a span (the `with vigil.start_span(...)` block exiting) never raises due to a
  delivery problem -- delivery hasn't even been attempted yet at that point.
- Automatic/background delivery (batch-size or interval triggered) never raises into your
  application; failures are recorded through the SDK's own logger (`logging.getLogger("vigil")`)
  as a warning and the failed batch is dropped.
- An **explicit** `vigil.flush()` call is the one place delivery failure is surfaced to you: it
  raises `vigil.VigilFlushError` if delivery ultimately fails (after retries), so you can decide
  how to handle it (e.g. log it yourself, alert, or ignore it). Either way, the batch is not
  retained after `flush()` returns -- V1 does not persist or re-queue a batch that failed to send.
- `vigil.close()` never raises for a delivery failure either (it logs, like background delivery) --
  it always completes so shutdown is never blocked by a broken backend.
- No SDK exception message ever includes your API key or any span's `input`/`output`/attribute
  content.

```python
vigil.flush()  # may raise VigilFlushError

with vigil.start_span(...):
    ...  # never raises merely because Vigil's backend is unavailable
```

## Flush and close

```python
vigil.flush()   # synchronous; raises VigilFlushError on ultimate failure
vigil.close()   # flush + shut down the background worker; never raises; idempotent
```

`close()` is safe to call more than once. `Vigil` also supports use as a context manager, which
calls `close()` on exit:

```python
with Vigil(api_key="vgl_...", base_url="http://127.0.0.1:8000") as vigil:
    with vigil.start_span("op"):
        ...
```

## V1 limitations

This SDK deliberately does **not** provide, in V1:

- OpenTelemetry auto-instrumentation, or any OpenTelemetry runtime dependency.
- Automatic instrumentation of OpenAI, LangChain, or any other library (spans are created
  explicitly, via `start_span`).
- Redis-backed buffering, a persistent/on-disk queue, or a distributed queue -- the buffer is
  purely in-memory and bounded, and is lost if the process crashes before it is flushed.
- Streaming or incomplete spans -- a span is only sent once its `with` block has exited; there is
  no "this call is still in progress" signal.
- Automatic PII/secret redaction of `input`/`output`/attributes -- whatever you pass to
  `set_input`/`set_output`/`set_attribute` is sent as-is (subject to the ingestion API's own
  truncation limits, which are authoritative and not duplicated client-side).
- An explicit `add_event(...)` API (the wire format has room for span events; this SDK does not
  yet expose a way to add them).

## Development

Run tests:

```bash
uv run pytest
```

Most tests fake HTTP via `httpx.MockTransport` and need no running server.
`tests/test_integration.py` is the one exception -- it exercises a real local `apps/api` and skips
itself automatically (with an explanatory reason) unless `VIGIL_SDK_INTEGRATION_API_KEY` is set and
that API is reachable.

Run Ruff:

```bash
uv run ruff check .
```
