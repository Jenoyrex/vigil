from __future__ import annotations

from vigil.ids import generate_span_id, generate_trace_id


def test_trace_id_is_32_lowercase_hex_characters() -> None:
    trace_id = generate_trace_id()
    assert len(trace_id) == 32
    assert trace_id == trace_id.lower()
    int(trace_id, 16)  # raises ValueError if not valid hex


def test_span_id_is_16_lowercase_hex_characters() -> None:
    span_id = generate_span_id()
    assert len(span_id) == 16
    assert span_id == span_id.lower()
    int(span_id, 16)


def test_trace_ids_are_unique() -> None:
    ids = {generate_trace_id() for _ in range(2000)}
    assert len(ids) == 2000


def test_span_ids_are_unique() -> None:
    ids = {generate_span_id() for _ in range(2000)}
    assert len(ids) == 2000
