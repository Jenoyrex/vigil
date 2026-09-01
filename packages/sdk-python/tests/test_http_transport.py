"""HTTP shape: auth header, endpoint path, request body shape, timestamps."""

from __future__ import annotations

from datetime import datetime


def test_sends_bearer_authorization_header(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory(api_key="vgl_abc123.supersecret")
    with vigil.start_span("op"):
        pass
    vigil.flush()
    request = recording_transport.requests[0]
    assert request.headers["authorization"] == "Bearer vgl_abc123.supersecret"


def test_posts_to_v1_traces(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op"):
        pass
    vigil.flush()
    request = recording_transport.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/traces"


def test_request_body_has_resource_and_spans_only(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op"):
        pass
    vigil.flush()
    body = recording_transport.bodies[0]
    assert set(body.keys()) == {"resource", "spans"}


def test_span_fields_match_the_ingestion_api_shape(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op", span_type="tool") as span:
        span.set_attribute("k", "v")
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["trace_id"] == span.trace_id
    assert sent["span_id"] == span.span_id
    assert sent["parent_span_id"] is None
    assert sent["name"] == "op"
    assert sent["span_type"] == "tool"
    assert sent["attributes"] == {"k": "v"}


def test_timestamps_are_iso8601_and_timezone_aware(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op"):
        pass
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    parsed_start = datetime.fromisoformat(sent["start_time"])
    parsed_end = datetime.fromisoformat(sent["end_time"])
    assert parsed_start.tzinfo is not None
    assert parsed_end.tzinfo is not None
