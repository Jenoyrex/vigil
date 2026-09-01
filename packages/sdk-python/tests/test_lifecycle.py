"""close()/flush() lifecycle: idempotency, context-manager support, and
correct HTTP-client shutdown.
"""

from __future__ import annotations


def test_close_is_idempotent(vigil_factory) -> None:
    vigil = vigil_factory()
    vigil.close()
    vigil.close()
    vigil.close()  # must not raise


def test_close_flushes_remaining_buffered_spans(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory(flush_interval=60)
    with vigil.start_span("op"):
        pass
    vigil.close()
    assert len(recording_transport.bodies) == 1
    assert len(recording_transport.bodies[0]["spans"]) == 1


def test_context_manager_closes_and_flushes_on_exit(vigil_factory, recording_transport) -> None:
    with vigil_factory(flush_interval=60) as vigil, vigil.start_span("op"):
        pass
    assert len(recording_transport.bodies) == 1


def test_span_started_after_close_is_dropped_not_raised(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    vigil.close()
    with vigil.start_span("op"):
        pass  # must not raise
    assert recording_transport.bodies == []


def test_http_client_is_closed_after_close(vigil_factory) -> None:
    vigil = vigil_factory()
    vigil.close()
    assert vigil._http.is_closed
