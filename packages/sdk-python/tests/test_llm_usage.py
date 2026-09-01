from __future__ import annotations


def test_record_llm_usage_all_fields(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("llm call", span_type="llm") as span:
        span.record_llm_usage(
            provider="openai",
            model="gpt-4o-mini",
            input_tokens=12,
            output_tokens=8,
            total_tokens=20,
            cost_usd=0.000123,
        )
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["llm_provider"] == "openai"
    assert sent["llm_model"] == "gpt-4o-mini"
    assert sent["llm_input_tokens"] == 12
    assert sent["llm_output_tokens"] == 8
    assert sent["llm_total_tokens"] == 20
    assert sent["llm_cost_usd"] == 0.000123


def test_record_llm_usage_unset_fields_default_to_null(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("llm call", span_type="llm"):
        pass
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["llm_provider"] is None
    assert sent["llm_model"] is None
    assert sent["llm_input_tokens"] is None
    assert sent["llm_output_tokens"] is None
    assert sent["llm_total_tokens"] is None
    assert sent["llm_cost_usd"] is None


def test_record_llm_usage_partial_calls_accumulate(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("llm call", span_type="llm") as span:
        span.record_llm_usage(provider="openai")
        span.record_llm_usage(model="gpt-4o-mini")
        span.record_llm_usage(input_tokens=5, output_tokens=2, total_tokens=7)
    vigil.flush()
    [sent] = recording_transport.bodies[0]["spans"]
    assert sent["llm_provider"] == "openai"
    assert sent["llm_model"] == "gpt-4o-mini"
    assert sent["llm_input_tokens"] == 5
    assert sent["llm_output_tokens"] == 2
    assert sent["llm_total_tokens"] == 7
