from __future__ import annotations

import pytest


def test_set_attribute_string(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        span.set_attribute("str_attr", "value")
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["attributes"]["str_attr"] == "value"


def test_set_attribute_numbers(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        span.set_attribute("int_attr", 42)
        span.set_attribute("float_attr", 3.14)
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["attributes"]["int_attr"] == 42
    assert sent["attributes"]["float_attr"] == 3.14


def test_set_attribute_boolean(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        span.set_attribute("flag", True)
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["attributes"]["flag"] is True


def test_set_attribute_overwrites_previous_value_for_same_key(
    vigil_factory, recording_transport
) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        span.set_attribute("k", "first")
        span.set_attribute("k", "second")
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["attributes"]["k"] == "second"


def test_set_attribute_rejects_unsupported_types(vigil_factory) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        with pytest.raises(TypeError):
            span.set_attribute("bad", {"nested": "dict"})
        with pytest.raises(TypeError):
            span.set_attribute("bad", [1, 2, 3])
        with pytest.raises(TypeError):
            span.set_attribute("bad", None)
