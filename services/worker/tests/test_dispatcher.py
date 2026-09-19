"""Unit tests for worker.dispatcher.Dispatcher.

Uses hand-rolled, thread-safe fake repositories/evaluator (keyed by job
identity, not FIFO queues) rather than tests/conftest.py's
fake_clickhouse_client/fake_postgres_connection -- those are FIFO-queue-based
and correct for single-call repository-level tests, but multiple jobs
running concurrently through a real ThreadPoolExecutor call their
repository methods in a genuinely non-deterministic interleaved order, so a
shared FIFO queue could hand one job's queued response to a different job.
Keying by the actual call parameters (span_id, job_id) avoids that
entirely and is the right level of fake for dispatcher-level orchestration
tests -- SQL/parameter correctness is already covered by each repository's
own unit tests (test_span_repository.py, test_evaluation_jobs_repository.py,
etc.), not re-tested here.

`Dispatcher` takes a `resource_provider` (see worker/resources.py), not
fixed repository instances -- these fakes have no thread-unsafe resource to
protect, so the same fixed `ExecutionResources` bundle is handed back on
every call via `contextlib.nullcontext`, proving fake-based dependency
injection still works unchanged under the new resource-ownership boundary.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from app.relevance import RelevanceEvaluatorInput
from app.types import EvaluationResult

from worker.dispatcher import Dispatcher, DispatchOutcome
from worker.postgres.repository import ClaimedJob
from worker.registry import EvaluatorRegistry
from worker.resources import ExecutionResources
from worker.timeouts import EvaluatorTimeoutError

PROJECT_ID = uuid.uuid4()
CREATED_AT = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


class FakeEvaluator:
    """Duck-typed `Evaluator`: records every call (thread-safe), optionally
    raises for specific input text, and optionally runs a hook before
    returning -- the hook is what the deterministic concurrency test uses
    to force genuine overlap.
    """

    name = "fake_evaluator"
    version = "0.1.0"

    def __init__(self, *, fail_for: frozenset[str] = frozenset(), on_call=None) -> None:
        self._fail_for = fail_for
        self._on_call = on_call
        self._lock = threading.Lock()
        self.calls: list[tuple[RelevanceEvaluatorInput, float | None]] = []

    def evaluate(
        self, evaluator_input: RelevanceEvaluatorInput, *, threshold: float | None = None
    ) -> EvaluationResult:
        with self._lock:
            self.calls.append((evaluator_input, threshold))
        if self._on_call is not None:
            self._on_call()
        if evaluator_input.input_text in self._fail_for:
            raise RuntimeError(f"synthetic evaluator failure for {evaluator_input.input_text!r}")
        return EvaluationResult(
            evaluator_name=self.name,
            evaluator_version=self.version,
            score=0.9,
            label="relevant",
            explanation="fake",
            evaluation_latency_ms=1.0,
            evaluation_cost_usd=None,
            evaluator_model="fake-model",
            evaluator_provider=None,
        )


class FakeSpanRepository:
    def __init__(self, spans_by_span_id: dict[str, dict]) -> None:
        self._spans = spans_by_span_id

    def get_span(self, *, project_id, trace_id, span_id):
        return self._spans.get(span_id)


class FakeEvaluatorConfigRepository:
    def get_config(self, *, project_id, evaluator_name):
        return None


class FakeResultsRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.inserted_rows: list[dict] = []

    def insert_results(self, rows: list[dict]) -> None:
        with self._lock:
            self.inserted_rows.extend(rows)


class FakeJobsRepository:
    """`mark_failed`/`mark_dead_letter` are `Mock()` spies (returning `True`
    by default, i.e. "recorded") so tests can assert exactly how and how
    often each was called -- including asserting zero calls, for the cases
    where neither should happen. `claim_jobs` is a `Mock()` with no return
    value configured; `Dispatcher` must never call it at all, so no test
    needs it to do anything.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mark_succeeded_calls: list[tuple[uuid.UUID, int]] = []
        self.mark_failed = Mock(return_value=True)
        self.mark_dead_letter = Mock(return_value=True)
        self.claim_jobs = Mock()

    def mark_succeeded(self, *, job_id: uuid.UUID, claimed_attempt_count: int) -> bool:
        with self._lock:
            self.mark_succeeded_calls.append((job_id, claimed_attempt_count))
        return True


def _job(
    *, span_id: str, attempt_count: int = 1, evaluator_name: str = "fake_evaluator"
) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        trace_id=uuid.uuid4().hex,
        span_id=span_id,
        evaluator_name=evaluator_name,
        evaluator_version="0.1.0",
        attempt_count=attempt_count,
        max_retries=3,
        created_at=CREATED_AT,
    )


def _span(text: str) -> dict:
    return {
        "input": text,
        "input_truncated": False,
        "output": f"{text} output",
        "output_truncated": False,
    }


def _make_dispatcher(
    *,
    max_concurrent_evaluations: int,
    evaluator: FakeEvaluator,
    spans: dict[str, dict],
    evaluator_call_timeout_seconds: float | None = None,
) -> tuple[Dispatcher, FakeResultsRepository, FakeJobsRepository]:
    results_repository = FakeResultsRepository()
    jobs_repository = FakeJobsRepository()
    resources = ExecutionResources(
        span_repository=FakeSpanRepository(spans),
        evaluator_config_repository=FakeEvaluatorConfigRepository(),
        results_repository=results_repository,
        jobs_repository=jobs_repository,
    )
    kwargs = {}
    if evaluator_call_timeout_seconds is not None:
        kwargs["evaluator_call_timeout_seconds"] = evaluator_call_timeout_seconds
    dispatcher = Dispatcher(
        max_concurrent_evaluations=max_concurrent_evaluations,
        registry=EvaluatorRegistry(evaluators=[evaluator]),
        resource_provider=lambda: nullcontext(resources),
        **kwargs,
    )
    return dispatcher, results_repository, jobs_repository


# -- construction --------------------------------------------------------


def test_rejects_a_non_positive_concurrency_limit() -> None:
    resources = ExecutionResources(
        span_repository=FakeSpanRepository({}),
        evaluator_config_repository=FakeEvaluatorConfigRepository(),
        results_repository=FakeResultsRepository(),
        jobs_repository=FakeJobsRepository(),
    )
    with pytest.raises(ValueError):
        Dispatcher(
            max_concurrent_evaluations=0,
            registry=EvaluatorRegistry(evaluators=[FakeEvaluator()]),
            resource_provider=lambda: nullcontext(resources),
        )


def test_dispatch_with_no_jobs_returns_empty_list() -> None:
    dispatcher, _, _ = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=FakeEvaluator(), spans={}
    )
    assert dispatcher.dispatch([]) == []


# -- batch dispatch / success bookkeeping ---------------------------------


def test_all_successful_jobs_complete_and_are_marked_succeeded() -> None:
    jobs = [_job(span_id=f"span-{i}") for i in range(5)]
    spans = {job.span_id: _span(f"text-{job.span_id}") for job in jobs}
    evaluator = FakeEvaluator()
    dispatcher, results_repository, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=3, evaluator=evaluator, spans=spans
    )

    outcomes = dispatcher.dispatch(jobs)

    assert len(outcomes) == 5
    assert all(outcome.succeeded for outcome in outcomes)
    assert len(results_repository.inserted_rows) == 5
    assert {job_id for job_id, _ in jobs_repository.mark_succeeded_calls} == {j.id for j in jobs}


def test_outcomes_are_returned_in_the_same_order_as_the_input_jobs() -> None:
    jobs = [_job(span_id=f"span-{i}") for i in range(4)]
    spans = {job.span_id: _span(f"text-{job.span_id}") for job in jobs}
    dispatcher, _, _ = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=FakeEvaluator(), spans=spans
    )

    outcomes = dispatcher.dispatch(jobs)
    assert [outcome.job.id for outcome in outcomes] == [job.id for job in jobs]


def test_execute_job_receives_the_original_job_including_attempt_count() -> None:
    jobs = [_job(span_id="span-a", attempt_count=1), _job(span_id="span-b", attempt_count=3)]
    spans = {job.span_id: _span(job.span_id) for job in jobs}
    dispatcher, _, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=FakeEvaluator(), spans=spans
    )

    dispatcher.dispatch(jobs)

    calls = dict(jobs_repository.mark_succeeded_calls)
    assert calls[jobs[0].id] == 1
    assert calls[jobs[1].id] == 3


# -- failure isolation / structured outcomes -------------------------------


def test_one_failed_job_does_not_prevent_others_from_completing() -> None:
    jobs = [_job(span_id=f"span-{i}") for i in range(3)]
    spans = {job.span_id: _span(job.span_id) for job in jobs}
    failing_text = spans[jobs[1].span_id]["input"]
    evaluator = FakeEvaluator(fail_for=frozenset({failing_text}))
    dispatcher, results_repository, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=evaluator, spans=spans
    )

    outcomes = dispatcher.dispatch(jobs)

    by_id = {outcome.job.id: outcome for outcome in outcomes}
    assert by_id[jobs[0].id].succeeded is True
    assert by_id[jobs[2].id].succeeded is True
    assert by_id[jobs[1].id].succeeded is False

    failed_outcome = by_id[jobs[1].id]
    assert failed_outcome.evaluation_outcome is None
    assert isinstance(failed_outcome.error, RuntimeError)

    succeeded_ids = {job_id for job_id, _ in jobs_repository.mark_succeeded_calls}
    assert succeeded_ids == {jobs[0].id, jobs[2].id}
    assert len(results_repository.inserted_rows) == 2


def test_missing_span_is_captured_as_a_structured_failure() -> None:
    from worker.execution import SourceSpanNotFoundError

    job = _job(span_id="missing-span")
    dispatcher, results_repository, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=FakeEvaluator(), spans={}
    )

    [outcome] = dispatcher.dispatch([job])

    assert outcome.succeeded is False
    assert isinstance(outcome.error, SourceSpanNotFoundError)
    assert results_repository.inserted_rows == []
    assert jobs_repository.mark_succeeded_calls == []


# -- dispatcher invokes failure handling on execute_job failure ------------
#
# Phase 3D's Dispatcher never called mark_failed/mark_dead_letter at all --
# a failed job's row was left untouched. Phase 3E's worker.failure_handling
# changes that: Dispatcher now calls handle_execution_failure (with the same
# task-local jobs_repository execute_job was given) whenever execute_job
# raises. These tests deliberately invert the Phase 3D-era assertion that
# used to live here (test_dispatcher_never_calls_mark_failed_or_mark_dead_letter)
# -- that premise no longer holds, by design.


def test_dispatcher_calls_mark_failed_for_a_retryable_failure_with_budget_remaining() -> None:
    jobs = [_job(span_id="span-a", attempt_count=1)]  # max_retries=3, well below cap
    spans = {jobs[0].span_id: _span(jobs[0].span_id)}
    evaluator = FakeEvaluator(fail_for=frozenset({spans[jobs[0].span_id]["input"]}))
    dispatcher, _, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=evaluator, spans=spans
    )

    [outcome] = dispatcher.dispatch(jobs)

    jobs_repository.mark_failed.assert_called_once()
    jobs_repository.mark_dead_letter.assert_not_called()
    call_kwargs = jobs_repository.mark_failed.call_args.kwargs
    assert call_kwargs["job_id"] == jobs[0].id
    assert call_kwargs["claimed_attempt_count"] == 1

    assert outcome.succeeded is False
    assert outcome.failure_handling is not None
    assert outcome.failure_handling.new_status == "failed"
    assert outcome.failure_handling.recorded is True


def test_dispatcher_calls_mark_dead_letter_when_retry_budget_exhausted() -> None:
    jobs = [_job(span_id="span-a", attempt_count=3)]  # max_retries=3 -- this IS the cap
    spans = {jobs[0].span_id: _span(jobs[0].span_id)}
    evaluator = FakeEvaluator(fail_for=frozenset({spans[jobs[0].span_id]["input"]}))
    dispatcher, _, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=evaluator, spans=spans
    )

    [outcome] = dispatcher.dispatch(jobs)

    jobs_repository.mark_dead_letter.assert_called_once()
    jobs_repository.mark_failed.assert_not_called()
    assert outcome.failure_handling.new_status == "dead_letter"


def test_dispatcher_dead_letters_a_permanent_failure_immediately() -> None:
    """A missing span is permanent (worker.failure_handling.is_retryable),
    so it must be dead-lettered on the very first attempt, regardless of
    how much retry budget remains."""
    job = _job(span_id="missing-span", attempt_count=1)  # max_retries=3, budget untouched
    dispatcher, _, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=FakeEvaluator(), spans={}
    )

    [outcome] = dispatcher.dispatch([job])

    jobs_repository.mark_dead_letter.assert_called_once()
    jobs_repository.mark_failed.assert_not_called()
    assert outcome.failure_handling.new_status == "dead_letter"


def test_dispatch_outcome_failure_handling_is_none_on_success() -> None:
    jobs = [_job(span_id="span-a")]
    spans = {jobs[0].span_id: _span(jobs[0].span_id)}
    dispatcher, _, _ = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=FakeEvaluator(), spans=spans
    )

    [outcome] = dispatcher.dispatch(jobs)

    assert outcome.succeeded is True
    assert outcome.failure_handling is None


def test_dispatcher_degrades_gracefully_when_recording_the_failure_itself_fails() -> None:
    """Compound failure: execute_job raises, and then recording that
    failure (mark_failed/mark_dead_letter) *also* raises -- e.g. PostgreSQL
    became unreachable at the exact moment of trying to record a
    ClickHouse-caused failure. dispatch() must still return a complete,
    non-crashing DispatchOutcome: the original error is preserved, and
    failure_handling reports None (could not be recorded) rather than
    raising out of dispatch() entirely.
    """
    job = _job(span_id="span-a", attempt_count=1)
    spans = {job.span_id: _span(job.span_id)}
    evaluator = FakeEvaluator(fail_for=frozenset({spans[job.span_id]["input"]}))
    dispatcher, _, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=evaluator, spans=spans
    )
    jobs_repository.mark_failed.side_effect = RuntimeError("PostgreSQL unreachable")

    [outcome] = dispatcher.dispatch([job])

    assert outcome.succeeded is False
    assert isinstance(outcome.error, RuntimeError)
    assert "synthetic evaluator failure" in str(outcome.error)
    assert outcome.failure_handling is None


def test_dispatch_outcome_has_the_expected_fields() -> None:
    """Structural guard, updated for Phase 3E: DispatchOutcome now carries
    `failure_handling` alongside the original three fields -- this replaces
    Phase 3D's `test_dispatch_outcome_has_no_retry_or_backoff_fields`, whose
    premise (no such field will ever exist) this phase deliberately
    supersedes.
    """
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(DispatchOutcome)}
    assert field_names == {"job", "evaluation_outcome", "error", "failure_handling"}


def test_dispatcher_never_claims_jobs() -> None:
    jobs = [_job(span_id="span-a")]
    spans = {job.span_id: _span(job.span_id) for job in jobs}
    dispatcher, _, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=FakeEvaluator(), spans=spans
    )

    dispatcher.dispatch(jobs)

    jobs_repository.claim_jobs.assert_not_called()


# -- resource-provider dependency injection ---------------------------------


def test_resource_provider_is_invoked_once_per_task() -> None:
    """Explicit proof that Dispatcher invokes the injected `resource_provider`
    itself, once per task -- not a fixed, pre-bound set of repositories held
    from construction time. Every other test in this file relies on this
    mechanism implicitly (via `_make_dispatcher`'s `nullcontext`-wrapped
    fixed bundle); this test uses its own custom provider to make the
    invocation count directly visible, proving fake-based dependency
    injection is not limited to `nullcontext` -- any context-manager
    factory works.
    """
    from contextlib import contextmanager

    jobs = [_job(span_id=f"span-{i}") for i in range(3)]
    spans = {job.span_id: _span(job.span_id) for job in jobs}
    results_repository = FakeResultsRepository()
    jobs_repository = FakeJobsRepository()
    span_repository = FakeSpanRepository(spans)
    evaluator_config_repository = FakeEvaluatorConfigRepository()

    lock = threading.Lock()
    call_count = 0

    @contextmanager
    def counting_provider():
        nonlocal call_count
        with lock:
            call_count += 1
        yield ExecutionResources(
            span_repository=span_repository,
            evaluator_config_repository=evaluator_config_repository,
            results_repository=results_repository,
            jobs_repository=jobs_repository,
        )

    dispatcher = Dispatcher(
        max_concurrent_evaluations=2,
        registry=EvaluatorRegistry(evaluators=[FakeEvaluator()]),
        resource_provider=counting_provider,
    )

    outcomes = dispatcher.dispatch(jobs)

    assert all(outcome.succeeded for outcome in outcomes)
    assert call_count == 3


# -- shared registry / evaluator reuse -------------------------------------


def test_the_same_evaluator_instance_handles_every_job() -> None:
    jobs = [_job(span_id=f"span-{i}") for i in range(4)]
    spans = {job.span_id: _span(job.span_id) for job in jobs}
    evaluator = FakeEvaluator()
    dispatcher, _, _ = _make_dispatcher(
        max_concurrent_evaluations=2, evaluator=evaluator, spans=spans
    )

    dispatcher.dispatch(jobs)

    # If a new evaluator were constructed per task, this one shared
    # instance's own call log would not show every job.
    assert len(evaluator.calls) == 4


# -- deterministic bounded concurrency --------------------------------------


def test_never_more_than_the_configured_limit_run_simultaneously() -> None:
    """Barrier + counter, not sleep/timing: each evaluate() call increments
    a shared counter, records the peak concurrent value under a lock, then
    rendezvouses on a `threading.Barrier` sized to exactly
    `max_concurrent_evaluations`. If the pool ever allowed *more* than that
    many concurrent calls, `peak` would exceed the limit (a hard
    assertion failure, not a timing coincidence). If the pool allowed
    *fewer* than that many concurrent calls (e.g. serialized execution),
    the barrier -- which only releases once exactly that many parties have
    called `wait()` -- would time out instead of returning, which is also
    a deterministic, unambiguous failure (not a flaky race).
    """
    max_concurrent = 3
    job_count = max_concurrent * 2  # two full rounds, no leftover party

    lock = threading.Lock()
    active = 0
    peak = 0
    barrier = threading.Barrier(max_concurrent, timeout=10)

    def _on_call() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1

    jobs = [_job(span_id=f"span-{i}") for i in range(job_count)]
    spans = {job.span_id: _span(job.span_id) for job in jobs}
    evaluator = FakeEvaluator(on_call=_on_call)
    dispatcher, _, _ = _make_dispatcher(
        max_concurrent_evaluations=max_concurrent, evaluator=evaluator, spans=spans
    )

    outcomes = dispatcher.dispatch(jobs)

    assert all(outcome.succeeded for outcome in outcomes)
    assert peak == max_concurrent


# -- evaluator call timeout (Phase 4A) ----------------------------------------


class HangingEvaluator:
    """An `Evaluator` test double whose `evaluate()` blocks on a
    `threading.Event` the test controls -- never a real, unbounded hang."""

    name = "hanging_evaluator"
    version = "0.1.0"

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def evaluate(
        self, evaluator_input: RelevanceEvaluatorInput, *, threshold: float | None = None
    ) -> EvaluationResult:
        self._release.wait(timeout=10.0)
        return EvaluationResult(
            evaluator_name=self.name,
            evaluator_version=self.version,
            score=0.9,
            label="relevant",
            explanation="fake",
            evaluation_latency_ms=1.0,
            evaluation_cost_usd=None,
            evaluator_model="fake-model",
            evaluator_provider=None,
        )


def test_dispatch_returns_within_bounded_time_when_the_evaluator_hangs() -> None:
    """The actual regression this item exists to fix: one hung `evaluate()`
    call must never wedge `Dispatcher.dispatch()` -- it must return
    (raising nothing itself; the hang becomes a per-job `DispatchOutcome.error`
    instead) within roughly `evaluator_call_timeout_seconds`, never for
    however long the hung call actually keeps running."""
    release = threading.Event()  # deliberately never set within this test's
    # own bounded window -- proving dispatch() doesn't wait for it regardless.
    evaluator = HangingEvaluator(release)
    job = _job(span_id="span-hang", evaluator_name=evaluator.name)
    spans = {job.span_id: _span(job.span_id)}
    dispatcher, _, _ = _make_dispatcher(
        max_concurrent_evaluations=1,
        evaluator=evaluator,
        spans=spans,
        evaluator_call_timeout_seconds=0.05,
    )

    started = time.monotonic()
    try:
        outcomes = dispatcher.dispatch([job])
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, "dispatch() must not block on a hung evaluator call"
        assert len(outcomes) == 1
    finally:
        release.set()


def test_evaluator_timeout_becomes_the_dispatch_outcomes_error() -> None:
    release = threading.Event()
    evaluator = HangingEvaluator(release)
    job = _job(span_id="span-hang", evaluator_name=evaluator.name)
    spans = {job.span_id: _span(job.span_id)}
    dispatcher, _, _ = _make_dispatcher(
        max_concurrent_evaluations=1,
        evaluator=evaluator,
        spans=spans,
        evaluator_call_timeout_seconds=0.05,
    )

    try:
        [outcome] = dispatcher.dispatch([job])
    finally:
        release.set()

    assert not outcome.succeeded
    assert isinstance(outcome.error, EvaluatorTimeoutError)


def test_evaluator_timeout_invokes_the_existing_failure_handler() -> None:
    """A timeout must be classified and recorded by the unmodified
    `worker.failure_handling` machinery, exactly like any other failure --
    no new classification code for this exception type."""
    release = threading.Event()
    evaluator = HangingEvaluator(release)
    job = _job(span_id="span-hang", evaluator_name=evaluator.name, attempt_count=1)
    spans = {job.span_id: _span(job.span_id)}
    dispatcher, _, jobs_repository = _make_dispatcher(
        max_concurrent_evaluations=1,
        evaluator=evaluator,
        spans=spans,
        evaluator_call_timeout_seconds=0.05,
    )

    try:
        [outcome] = dispatcher.dispatch([job])
    finally:
        release.set()

    # attempt_count (1) < max_retries (3) -- retryable, not exhausted:
    # mark_failed, not mark_dead_letter.
    assert outcome.failure_handling is not None
    assert outcome.failure_handling.new_status == "failed"
    jobs_repository.mark_failed.assert_called_once()
    jobs_repository.mark_dead_letter.assert_not_called()


def test_a_sibling_fast_job_still_succeeds_despite_another_job_hanging() -> None:
    """The hang must not starve or corrupt any other concurrently-dispatched
    job in the same batch."""
    release = threading.Event()
    hanging_evaluator = HangingEvaluator(release)
    fast_evaluator = FakeEvaluator()

    hang_job = _job(span_id="span-hang", evaluator_name=hanging_evaluator.name)
    fast_job = _job(span_id="span-fast", evaluator_name=fast_evaluator.name)
    spans = {
        hang_job.span_id: _span(hang_job.span_id),
        fast_job.span_id: _span(fast_job.span_id),
    }

    results_repository = FakeResultsRepository()
    jobs_repository = FakeJobsRepository()
    resources = ExecutionResources(
        span_repository=FakeSpanRepository(spans),
        evaluator_config_repository=FakeEvaluatorConfigRepository(),
        results_repository=results_repository,
        jobs_repository=jobs_repository,
    )
    registry = EvaluatorRegistry(evaluators=[hanging_evaluator, fast_evaluator])
    dispatcher = Dispatcher(
        max_concurrent_evaluations=2,
        registry=registry,
        resource_provider=lambda: nullcontext(resources),
        evaluator_call_timeout_seconds=0.05,
    )

    try:
        outcomes = dispatcher.dispatch([hang_job, fast_job])
    finally:
        release.set()

    outcomes_by_span_id = {outcome.job.span_id: outcome for outcome in outcomes}
    assert not outcomes_by_span_id["span-hang"].succeeded
    assert outcomes_by_span_id["span-fast"].succeeded
    assert len(jobs_repository.mark_succeeded_calls) == 1
