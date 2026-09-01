"""The most important invariant: telemetry delivery failures must never
raise into the caller's application code, except from an explicit,
synchronous `flush()` call (covered in test_retries.py)."""

from __future__ import annotations

import time

import httpx


def test_span_completion_never_raises_when_the_backend_is_down(
    vigil_factory, recording_transport
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    recording_transport.handler = handler
    vigil = vigil_factory(max_batch_size=1, max_retries=0, flush_interval=60)

    # Reaching max_batch_size wakes the background worker, whose delivery
    # attempt will fail -- exiting this `with` block must not raise.
    with vigil.start_span("op") as span:
        span.set_attribute("k", "v")

    assert span.status == "unset"  # the span itself is unaffected


def test_background_delivery_failure_is_only_logged(
    vigil_factory, recording_transport, caplog
) -> None:
    recording_transport.handler = lambda request: httpx.Response(500)
    vigil = vigil_factory(max_batch_size=1, max_retries=0, flush_interval=60)

    with caplog.at_level("WARNING", logger="vigil"):
        with vigil.start_span("op"):
            pass

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not recording_transport.requests:
            time.sleep(0.01)

    assert len(recording_transport.requests) == 1
    assert any("delivery failed" in record.message for record in caplog.records)


def test_close_never_raises_when_the_backend_is_down(vigil_factory, recording_transport) -> None:
    recording_transport.handler = lambda request: httpx.Response(503)
    vigil = vigil_factory(max_retries=0, flush_interval=60)
    with vigil.start_span("op"):
        pass
    vigil.close()  # must not raise, even though delivery ultimately fails
