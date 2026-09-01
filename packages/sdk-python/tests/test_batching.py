"""Batch buffering: the pure `BoundedBatchBuffer` logic, plus the wiring
that connects it to `Vigil.start_span`/`flush`/the background worker.
"""

from __future__ import annotations

import time

from vigil.batching import BoundedBatchBuffer

# -- BoundedBatchBuffer: deterministic, no threading involved ---------------


def test_buffer_add_signals_flush_once_max_batch_size_is_reached() -> None:
    buffer = BoundedBatchBuffer(max_batch_size=2, max_queue_size=10)
    added1, should_flush1 = buffer.add({"span": 1})
    assert added1 is True
    assert should_flush1 is False
    added2, should_flush2 = buffer.add({"span": 2})
    assert added2 is True
    assert should_flush2 is True


def test_buffer_drops_items_once_max_queue_size_is_reached() -> None:
    buffer = BoundedBatchBuffer(max_batch_size=100, max_queue_size=2)
    assert buffer.add({"span": 1})[0] is True
    assert buffer.add({"span": 2})[0] is True
    assert buffer.add({"span": 3})[0] is False
    assert len(buffer) == 2


def test_buffer_drain_empties_the_buffer_and_returns_items_in_order() -> None:
    buffer = BoundedBatchBuffer(max_batch_size=100, max_queue_size=100)
    buffer.add({"span": 1})
    buffer.add({"span": 2})
    assert buffer.drain() == [{"span": 1}, {"span": 2}]
    assert buffer.drain() == []
    assert len(buffer) == 0


# -- End-to-end wiring through Vigil -----------------------------------------


def test_multiple_spans_are_sent_in_a_single_request(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory(max_batch_size=10, flush_interval=60)
    for i in range(3):
        with vigil.start_span(f"op-{i}"):
            pass
    vigil.flush()
    assert len(recording_transport.bodies) == 1
    assert len(recording_transport.bodies[0]["spans"]) == 3


def test_explicit_flush_sends_a_partial_batch(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory(max_batch_size=10, flush_interval=60)
    with vigil.start_span("only-one"):
        pass
    vigil.flush()
    assert len(recording_transport.bodies) == 1
    assert len(recording_transport.bodies[0]["spans"]) == 1


def test_flush_with_nothing_buffered_sends_no_request(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    vigil.flush()
    assert recording_transport.bodies == []


def test_reaching_max_batch_size_triggers_a_background_flush(
    vigil_factory, recording_transport
) -> None:
    vigil = vigil_factory(max_batch_size=2, flush_interval=60)
    with vigil.start_span("a"):
        pass
    with vigil.start_span("b"):
        pass

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not recording_transport.bodies:
        time.sleep(0.01)

    assert len(recording_transport.bodies) == 1
    assert len(recording_transport.bodies[0]["spans"]) == 2


def test_max_queue_size_bounds_the_buffer_end_to_end(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory(max_batch_size=1000, max_queue_size=2, flush_interval=60)
    for i in range(5):
        with vigil.start_span(f"op-{i}"):
            pass
    vigil.flush()
    assert len(recording_transport.bodies[0]["spans"]) == 2
