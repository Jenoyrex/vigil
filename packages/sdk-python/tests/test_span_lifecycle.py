"""Span creation/completion, timestamps, and trace/parent propagation."""

from __future__ import annotations

import time

import pytest


def test_span_records_timezone_aware_start_and_end_time(vigil_factory) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        assert span.start_time is not None
        assert span.start_time.tzinfo is not None
        time.sleep(0.01)
    assert span.end_time is not None
    assert span.end_time.tzinfo is not None


def test_end_time_is_after_or_equal_to_start_time(vigil_factory) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op") as span:
        time.sleep(0.01)
    assert span.end_time >= span.start_time
    assert (span.end_time - span.start_time).total_seconds() >= 0.01


def test_root_span_generates_new_trace_with_no_parent(vigil_factory) -> None:
    vigil = vigil_factory()
    with vigil.start_span("root") as span:
        assert len(span.trace_id) == 32
        assert span.parent_span_id is None


def test_nested_span_inherits_trace_id_and_parent_span_id(vigil_factory) -> None:
    vigil = vigil_factory()
    with (
        vigil.start_span("agent") as agent,
        vigil.start_span("retrieval", span_type="retrieval") as retrieval,
    ):
        assert retrieval.trace_id == agent.trace_id
        assert retrieval.parent_span_id == agent.span_id
        assert retrieval.span_id != agent.span_id


def test_sibling_spans_after_a_nested_block_still_share_the_same_parent(vigil_factory) -> None:
    vigil = vigil_factory()
    with vigil.start_span("agent") as agent:
        with vigil.start_span("retrieval", span_type="retrieval"):
            pass
        with vigil.start_span("llm call", span_type="llm") as llm:
            assert llm.trace_id == agent.trace_id
            assert llm.parent_span_id == agent.span_id


def test_deeply_nested_spans_propagate_through_multiple_levels(vigil_factory) -> None:
    vigil = vigil_factory()
    with (
        vigil.start_span("agent") as agent,
        vigil.start_span("tool", span_type="tool") as tool,
        vigil.start_span("http", span_type="http") as http_span,
    ):
        assert http_span.trace_id == agent.trace_id
        assert http_span.parent_span_id == tool.span_id
        assert tool.parent_span_id == agent.span_id


def test_span_started_after_a_root_span_completes_starts_a_new_trace(vigil_factory) -> None:
    vigil = vigil_factory()
    with vigil.start_span("first") as first:
        pass
    with vigil.start_span("second") as second:
        assert second.trace_id != first.trace_id
        assert second.parent_span_id is None


def test_completed_span_is_delivered_on_flush(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op"):
        pass
    vigil.flush()
    assert len(recording_transport.bodies) == 1
    [sent_span] = recording_transport.bodies[0]["spans"]
    assert sent_span["name"] == "op"


def test_exception_inside_span_sets_error_status_and_still_propagates(vigil_factory) -> None:
    vigil = vigil_factory()
    with pytest.raises(ValueError, match="boom"), vigil.start_span("op") as span:
        raise ValueError("boom")
    assert span.status == "error"
    assert span.status_message is not None
    assert "boom" in span.status_message


def test_explicit_status_is_not_overwritten_by_a_later_exception(vigil_factory) -> None:
    vigil = vigil_factory()
    with pytest.raises(ValueError), vigil.start_span("op") as span:
        span.set_status("ok")
        raise ValueError("boom")
    assert span.status == "ok"
