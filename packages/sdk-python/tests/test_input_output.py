from __future__ import annotations


def test_set_input_and_output_strings(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        span.set_input("hello")
        span.set_output("world")
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["input"] == "hello"
    assert sent["output"] == "world"


def test_set_input_and_output_json_compatible_structures(
    vigil_factory, recording_transport
) -> None:
    vigil = vigil_factory()
    payload_in = {"messages": [{"role": "user", "content": "hi"}]}
    payload_out = {"role": "assistant", "content": "hi there"}
    with vigil.start_span("op") as span:
        span.set_input(payload_in)
        span.set_output(payload_out)
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["input"] == payload_in
    assert sent["output"] == payload_out


def test_unset_input_and_output_serialize_to_null(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op"):
        pass
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["input"] is None
    assert sent["output"] is None


def test_unicode_content_is_preserved_exactly(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    text = "héllo wörld 你好 🎉"
    with vigil.start_span("op") as span:
        span.set_input(text)
        span.set_output({"echo": text})
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["input"] == text
    assert sent["output"] == {"echo": text}
