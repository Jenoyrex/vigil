from __future__ import annotations

import pytest


def test_default_status_is_unset(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op"):
        pass
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["status"] == "unset"
    assert sent["status_message"] is None


def test_set_status_ok(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        span.set_status("ok")
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["status"] == "ok"


def test_set_status_error_with_message(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        span.set_status("error", "something failed")
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["status"] == "error"
    assert sent["status_message"] == "something failed"


def test_set_status_rejects_invalid_value(vigil_factory) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span, pytest.raises(ValueError):
        span.set_status("bogus")
