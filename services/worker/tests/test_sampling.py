"""Unit tests for worker.sampling.is_sampled_in.

The canonical test of the deterministic-sampling algorithm -- see
worker/sampling.py's own docstring for why apps/api independently
implements (never imports) the identical algorithm, whose behavior these
tests indirectly hold that copy to as well.
"""

from __future__ import annotations

import uuid

import pytest

from worker.sampling import is_sampled_in

PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


def _key(**overrides) -> dict:
    defaults = {
        "project_id": PROJECT_ID,
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "evaluator_name": "relevance",
        "sampling_rate": 0.5,
    }
    return {**defaults, **overrides}


def test_deterministic_across_repeated_calls() -> None:
    results = {is_sampled_in(**_key()) for _ in range(50)}
    assert len(results) == 1


def test_deterministic_across_many_distinct_process_like_invocations() -> None:
    """Simulates "many independent evaluations of the same key," which is
    what actually matters operationally (multiple poller processes, or one
    process restarted) -- Python's hash() would vary across real process
    restarts (PYTHONHASHSEED), but sha256-based is_sampled_in must not."""
    first = is_sampled_in(**_key())
    for _ in range(200):
        assert is_sampled_in(**_key()) == first


def test_deterministic_across_evaluator_version_changes() -> None:
    """evaluator_version is deliberately not part of the sampling key at
    all -- structurally, not just incidentally: is_sampled_in has no
    evaluator_version parameter, so a version bump literally cannot change
    the sampling decision for the same (project_id, trace_id, span_id,
    evaluator_name), because there is nowhere for it to be threaded in.
    """
    with pytest.raises(TypeError):
        is_sampled_in(**_key(), evaluator_version="0.2.0")  # type: ignore[call-arg]


def test_evaluator_name_participates_in_the_key() -> None:
    """Every other field held fixed, only evaluator_name varies across 20
    synthetic names at rate=0.5: if evaluator_name were ignored, every call
    would return the identical (fixed-inputs-determined) result. Getting
    both True and False proves it genuinely perturbs the hash, not just
    that the function runs without crashing."""
    results = {is_sampled_in(**_key(evaluator_name=f"evaluator-{i}")) for i in range(20)}
    assert results == {True, False}


def test_rate_zero_never_samples_in() -> None:
    for i in range(100):
        assert is_sampled_in(**_key(span_id=f"{i:016x}", sampling_rate=0.0)) is False


def test_negative_rate_never_samples_in() -> None:
    assert is_sampled_in(**_key(sampling_rate=-0.5)) is False


def test_rate_one_always_samples_in() -> None:
    for i in range(100):
        assert is_sampled_in(**_key(span_id=f"{i:016x}", sampling_rate=1.0)) is True


def test_rate_above_one_always_samples_in() -> None:
    assert is_sampled_in(**_key(sampling_rate=1.5)) is True


def test_statistical_sanity_at_half_rate() -> None:
    """Not a proof of uniformity, just a sanity check that ~half of a large,
    varied population is sampled in at rate=0.5 -- a badly broken hash
    (e.g. constant, or only varying in bits that get truncated) would fail
    this by a wide margin."""
    sampled_in = sum(
        is_sampled_in(
            project_id=PROJECT_ID,
            trace_id=TRACE_ID,
            span_id=f"{i:016x}",
            evaluator_name="relevance",
            sampling_rate=0.5,
        )
        for i in range(5000)
    )
    fraction = sampled_in / 5000
    assert 0.45 <= fraction <= 0.55


def test_project_id_participates_in_the_key() -> None:
    """Same reasoning as evaluator_name above, for project_id: two projects
    observing the identical trace_id/span_id/evaluator_name are not forced
    to the same sampling decision."""
    results = {is_sampled_in(**_key(project_id=uuid.uuid4())) for _ in range(20)}
    assert results == {True, False}
