"""Retry policy: transient failures are retried with bounded backoff;
permanent failures are not; retried requests resend identical span IDs.
"""

from __future__ import annotations

import httpx
import pytest

from vigil.exceptions import VigilFlushError


def _fail_once_then_succeed(first_response: httpx.Response):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return first_response
        return httpx.Response(200, json={"accepted": 1, "request_id": "r2"})

    return handler, state


def test_retries_on_503_then_succeeds(vigil_factory, recording_transport) -> None:
    handler, state = _fail_once_then_succeed(httpx.Response(503))
    recording_transport.handler = handler
    vigil = vigil_factory(max_retries=3)
    with vigil.start_span("op"):
        pass
    vigil.flush()  # must not raise
    assert state["calls"] == 2


def test_retries_on_500(vigil_factory, recording_transport) -> None:
    handler, state = _fail_once_then_succeed(httpx.Response(500))
    recording_transport.handler = handler
    vigil = vigil_factory(max_retries=3)
    with vigil.start_span("op"):
        pass
    vigil.flush()
    assert state["calls"] == 2


def test_retries_on_network_error_then_succeeds(vigil_factory, recording_transport) -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"accepted": 1, "request_id": "r2"})

    recording_transport.handler = handler
    vigil = vigil_factory(max_retries=3)
    with vigil.start_span("op"):
        pass
    vigil.flush()
    assert state["calls"] == 2


def test_retries_on_timeout_then_succeeds(vigil_factory, recording_transport) -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"accepted": 1, "request_id": "r2"})

    recording_transport.handler = handler
    vigil = vigil_factory(max_retries=3)
    with vigil.start_span("op"):
        pass
    vigil.flush()
    assert state["calls"] == 2


@pytest.mark.parametrize("status_code", [401, 403, 422])
def test_permanent_4xx_failures_are_not_retried(
    vigil_factory, recording_transport, status_code
) -> None:
    recording_transport.handler = lambda request: httpx.Response(
        status_code, json={"detail": "no"}
    )
    vigil = vigil_factory(max_retries=3)
    with vigil.start_span("op"):
        pass
    with pytest.raises(VigilFlushError):
        vigil.flush()
    assert len(recording_transport.requests) == 1


def test_exhausted_retries_raises_vigil_flush_error(vigil_factory, recording_transport) -> None:
    recording_transport.handler = lambda request: httpx.Response(503)
    vigil = vigil_factory(max_retries=2)
    with vigil.start_span("op"):
        pass
    with pytest.raises(VigilFlushError) as exc_info:
        vigil.flush()
    assert len(recording_transport.requests) == 3  # 1 initial attempt + 2 retries
    assert exc_info.value.dropped_spans == 1


def test_flush_error_message_never_contains_the_api_key(vigil_factory, recording_transport) -> None:
    recording_transport.handler = lambda request: httpx.Response(503)
    vigil = vigil_factory(api_key="vgl_secretprefix.supersecretvalue", max_retries=0)
    with vigil.start_span("op"):
        pass
    with pytest.raises(VigilFlushError) as exc_info:
        vigil.flush()
    assert "supersecretvalue" not in str(exc_info.value)


def test_retry_resends_identical_trace_and_span_ids(vigil_factory, recording_transport) -> None:
    handler, _ = _fail_once_then_succeed(httpx.Response(503))
    recording_transport.handler = handler
    vigil = vigil_factory(max_retries=3)
    with vigil.start_span("op") as span:
        pass
    vigil.flush()
    assert len(recording_transport.bodies) == 2
    first_sent = recording_transport.bodies[0]["spans"][0]
    second_sent = recording_transport.bodies[1]["spans"][0]
    assert first_sent["trace_id"] == second_sent["trace_id"] == span.trace_id
    assert first_sent["span_id"] == second_sent["span_id"] == span.span_id
