"""Tests for worker.adapters.span_to_relevance_input -- pure function, no
fakes or network needed."""

from __future__ import annotations

from app.relevance import RelevanceEvaluatorInput

from worker.adapters import span_to_relevance_input


def test_maps_input_and_output_text_directly() -> None:
    span = {
        "input": "What is the capital of France?",
        "input_truncated": False,
        "output": "Paris.",
        "output_truncated": False,
    }
    result = span_to_relevance_input(span)

    assert result == RelevanceEvaluatorInput(
        input_text="What is the capital of France?", output_text="Paris."
    )


def test_null_input_becomes_empty_string() -> None:
    span = {"input": None, "input_truncated": False, "output": "Paris.", "output_truncated": False}
    result = span_to_relevance_input(span)
    assert result.input_text == ""
    assert result.output_text == "Paris."


def test_null_output_becomes_empty_string() -> None:
    span = {
        "input": "What is the capital of France?",
        "input_truncated": False,
        "output": None,
        "output_truncated": False,
    }
    result = span_to_relevance_input(span)
    assert result.input_text == "What is the capital of France?"
    assert result.output_text == ""


def test_both_null_becomes_two_empty_strings() -> None:
    span = {"input": None, "input_truncated": False, "output": None, "output_truncated": False}
    result = span_to_relevance_input(span)
    assert result == RelevanceEvaluatorInput(input_text="", output_text="")


def test_json_flattened_input_passes_through_as_is() -> None:
    """The ClickHouse `input`/`output` columns already hold the
    ingestion-flattened text (a plain string verbatim, any other
    JSON-serializable value as its compact `json.dumps(...)` string) -- the
    adapter does no further parsing of it."""
    span = {
        "input": '{"messages":[{"role":"user","content":"hi"}]}',
        "input_truncated": False,
        "output": "hello!",
        "output_truncated": False,
    }
    result = span_to_relevance_input(span)
    assert result.input_text == '{"messages":[{"role":"user","content":"hi"}]}'
