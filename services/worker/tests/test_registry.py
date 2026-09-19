"""Tests for worker.registry.EvaluatorRegistry.

Uses one module-scoped real registry (constructing both production
evaluators, including `EmbeddingRelevanceEvaluator`'s ONNX session load) --
mirroring services/evaluator/tests/test_embedding_relevance.py's own
module-scoped fixture, so that load cost is paid at most once per test run.

The lazy-construction/timeout/race tests below use fresh, function-scoped
registries with fake, fast, controllable factories (via the `factories=`
constructor parameter) instead -- exercising timing-sensitive behavior
against the real, slow `EmbeddingRelevanceEvaluator` would be both slow and
flaky. No real infinite hang anywhere: every "slow" fake factory blocks on
a `threading.Event` that the test itself releases, so every test is
bounded.
"""

from __future__ import annotations

import threading
import time

import pytest
from app.embedding_relevance import EVALUATOR_NAME as EMBEDDING_NAME
from app.embedding_relevance import EVALUATOR_VERSION as EMBEDDING_VERSION
from app.relevance import EVALUATOR_NAME as RELEVANCE_NAME
from app.relevance import EVALUATOR_VERSION as RELEVANCE_VERSION
from app.relevance import RelevanceEvaluatorInput

from worker.registry import EvaluatorRegistry, UnknownEvaluatorError
from worker.timeouts import EvaluatorTimeoutError, outstanding_orphaned_calls

FAKE_KEY = ("fake", "0.1.0")


class _FakeEvaluator:
    name = "fake"
    version = "0.1.0"

    def evaluate(self, evaluator_input, *, threshold=None):  # noqa: ANN001, ANN201
        raise NotImplementedError("not exercised by these tests")


def _poll_until(predicate, *, timeout_seconds: float = 2.0, interval_seconds: float = 0.01) -> None:
    """Polls `predicate` until it's true or `timeout_seconds` elapses --
    used only to observe an already-bounded background thread's own
    completion (never to wait out a real hang)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_seconds)
    raise AssertionError(f"condition not met within {timeout_seconds}s")


@pytest.fixture(scope="module")
def registry() -> EvaluatorRegistry:
    return EvaluatorRegistry()


def test_registers_exactly_the_two_production_relevance_evaluators(
    registry: EvaluatorRegistry,
) -> None:
    assert registry.registered_keys() == {
        (RELEVANCE_NAME, RELEVANCE_VERSION),
        (EMBEDDING_NAME, EMBEDDING_VERSION),
    }


def test_get_returns_the_tfidf_relevance_evaluator(registry: EvaluatorRegistry) -> None:
    evaluator = registry.get(RELEVANCE_NAME, RELEVANCE_VERSION)
    assert evaluator.name == RELEVANCE_NAME
    assert evaluator.version == RELEVANCE_VERSION


def test_get_returns_the_embedding_relevance_evaluator(registry: EvaluatorRegistry) -> None:
    evaluator = registry.get(EMBEDDING_NAME, EMBEDDING_VERSION)
    assert evaluator.name == EMBEDDING_NAME
    assert evaluator.version == EMBEDDING_VERSION


def test_get_raises_for_unknown_evaluator_name(registry: EvaluatorRegistry) -> None:
    with pytest.raises(UnknownEvaluatorError):
        registry.get("groundedness", "0.1.0")


def test_get_raises_for_unregistered_version_of_a_known_evaluator(
    registry: EvaluatorRegistry,
) -> None:
    """Version skew during a rolling deploy: a worker fleet that no longer
    ships (or never shipped) this exact version must not silently fall
    back to whatever version it does have -- see registry.py's
    `UnknownEvaluatorError` docstring."""
    with pytest.raises(UnknownEvaluatorError):
        registry.get(RELEVANCE_NAME, "99.0.0")


def test_get_returns_the_same_instance_across_repeated_lookups(registry: EvaluatorRegistry) -> None:
    """Instance reuse: the whole point of registering once at construction
    time is that repeated dispatch never re-constructs (and, for the
    embedding evaluator, never reloads the ONNX session)."""
    first = registry.get(EMBEDDING_NAME, EMBEDDING_VERSION)
    second = registry.get(EMBEDDING_NAME, EMBEDDING_VERSION)
    assert first is second


def test_threshold_is_passed_per_call_without_mutating_the_shared_instance(
    registry: EvaluatorRegistry,
) -> None:
    """The same registry-held instance, looked up twice, must honor a
    different per-call threshold each time with no leakage between calls --
    this is what actually lets one instance serve many projects'
    independently configured thresholds."""
    evaluator = registry.get(RELEVANCE_NAME, RELEVANCE_VERSION)
    evaluator_input = RelevanceEvaluatorInput(
        input_text="What is the capital of France?",
        output_text="The capital of France is Paris.",
    )

    high_threshold_result = evaluator.evaluate(evaluator_input, threshold=0.999999)
    assert high_threshold_result.label == "not_relevant"

    low_threshold_result = evaluator.evaluate(evaluator_input, threshold=0.0)
    assert low_threshold_result.label == "relevant"

    # No override -> back to this instance's own unmutated default
    # (DEFAULT_THRESHOLD = 0.5; this pair's TF-IDF cosine similarity is
    # already independently asserted >= 0.5 in
    # services/evaluator/tests/test_relevance.py).
    default_result = evaluator.evaluate(evaluator_input)
    assert default_result.label == "relevant"
    assert evaluator is registry.get(RELEVANCE_NAME, RELEVANCE_VERSION)


# -- lazy construction (Phase 4A) --------------------------------------------


def test_fresh_registry_does_not_instantiate_the_embedding_evaluator() -> None:
    """The whole point of laziness: constructing the registry must never by
    itself trigger `EmbeddingRelevanceEvaluator.__init__` (the ONNX
    session load). Proven here by never calling `.get()` for it at all and
    relying on `EvaluatorRegistry()`'s own construction being effectively
    instant -- if it eagerly loaded the model, this test alone would take
    the same multi-second load time the module-scoped `registry` fixture
    above pays exactly once; it does not."""
    started = time.monotonic()
    fresh_registry = EvaluatorRegistry()
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, "EvaluatorRegistry() must not eagerly load any evaluator"
    assert fresh_registry.registered_keys() == {
        (RELEVANCE_NAME, RELEVANCE_VERSION),
        (EMBEDDING_NAME, EMBEDDING_VERSION),
    }


def test_registered_keys_available_before_any_construction() -> None:
    fresh_registry = EvaluatorRegistry(
        factories={FAKE_KEY: _FakeEvaluator}, evaluator_init_timeout_seconds=5.0
    )
    assert fresh_registry.registered_keys() == {FAKE_KEY}


def test_first_get_constructs_lazily() -> None:
    call_count = 0

    def _factory() -> _FakeEvaluator:
        nonlocal call_count
        call_count += 1
        return _FakeEvaluator()

    fresh_registry = EvaluatorRegistry(
        factories={FAKE_KEY: _factory}, evaluator_init_timeout_seconds=5.0
    )
    assert call_count == 0

    fresh_registry.get(*FAKE_KEY)
    assert call_count == 1


def test_repeated_get_returns_the_same_instance_and_constructs_once() -> None:
    call_count = 0

    def _factory() -> _FakeEvaluator:
        nonlocal call_count
        call_count += 1
        return _FakeEvaluator()

    fresh_registry = EvaluatorRegistry(
        factories={FAKE_KEY: _factory}, evaluator_init_timeout_seconds=5.0
    )
    first = fresh_registry.get(*FAKE_KEY)
    second = fresh_registry.get(*FAKE_KEY)
    third = fresh_registry.get(*FAKE_KEY)

    assert first is second is third
    assert call_count == 1


def test_concurrent_get_for_the_same_key_constructs_exactly_once() -> None:
    call_count_lock = threading.Lock()
    call_count = 0
    release = threading.Event()

    def _factory() -> _FakeEvaluator:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        # Every concurrent caller's construction attempt (if more than one
        # ever ran) would overlap here -- release is set only after every
        # thread has had a chance to enter the factory, so a bug that let
        # two threads both call the factory concurrently would be caught
        # as call_count == 2 below, not masked by one finishing before the
        # other even starts.
        release.wait(timeout=2.0)
        return _FakeEvaluator()

    fresh_registry = EvaluatorRegistry(
        factories={FAKE_KEY: _factory}, evaluator_init_timeout_seconds=5.0
    )

    results: list[_FakeEvaluator] = []
    results_lock = threading.Lock()

    def _call_get() -> None:
        instance = fresh_registry.get(*FAKE_KEY)
        with results_lock:
            results.append(instance)

    threads = [threading.Thread(target=_call_get) for _ in range(5)]
    for thread in threads:
        thread.start()

    # Give every thread a chance to reach (and block inside) the factory
    # before releasing it -- if the lock were ever bypassed, more than one
    # thread would reach `call_count += 1` during this window.
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(timeout=5.0)

    assert call_count == 1
    assert len(results) == 5
    assert len({id(r) for r in results}) == 1


def test_initialization_timeout_raises_and_releases_the_lock() -> None:
    release = threading.Event()

    def _slow_factory() -> _FakeEvaluator:
        release.wait(timeout=5.0)
        return _FakeEvaluator()

    fresh_registry = EvaluatorRegistry(
        factories={FAKE_KEY: _slow_factory}, evaluator_init_timeout_seconds=0.05
    )

    baseline_orphans = outstanding_orphaned_calls()
    with pytest.raises(EvaluatorTimeoutError):
        fresh_registry.get(*FAKE_KEY)
    assert outstanding_orphaned_calls() == baseline_orphans + 1

    # The lock must not still be held -- a second, independent attempt
    # (mirroring a retried job after backoff) must be able to proceed
    # immediately, not block waiting on the first, abandoned attempt.
    started = time.monotonic()
    with pytest.raises(EvaluatorTimeoutError):
        fresh_registry.get(*FAKE_KEY)
    assert time.monotonic() - started < 1.0, "a second attempt must not block on the first's lock"

    # Let the abandoned factories finish so the orphan count returns to
    # baseline and the background threads don't outlive this test.
    release.set()
    _poll_until(lambda: outstanding_orphaned_calls() == baseline_orphans)


class _WriteCountingInstances(dict):
    """Stands in for `EvaluatorRegistry`'s own `_instances` cache dict.
    Signals `on_second_write` the instant a *second* write to this dict
    completes -- via whichever dict method actually performs it
    (`setdefault`, today's implementation, or a plain `__setitem__`, so this
    still catches a future regression to unconditional overwrite) -- giving
    a test a deterministic way to wait for a late, abandoned construction
    attempt's own cache write to have actually landed, rather than merely
    for the factory function that precedes it to have returned (which
    happens *before* the write, and would let a test that only synchronizes
    on that moment pass even against a buggy implementation)."""

    def __init__(self, *, on_second_write: threading.Event) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._write_count = 0
        self._on_second_write = on_second_write

    def _note_write(self) -> None:
        with self._lock:
            self._write_count += 1
            if self._write_count == 2:
                self._on_second_write.set()

    def setdefault(self, key, default=None):  # noqa: ANN001, ANN201, D102
        result = super().setdefault(key, default)
        self._note_write()
        return result

    def __setitem__(self, key, value) -> None:  # noqa: D105
        super().__setitem__(key, value)
        self._note_write()


def test_late_successful_construction_does_not_replace_an_already_cached_instance() -> None:
    """The abandoned (timed-out) attempt's own construction eventually
    succeeds *after* a second, separate attempt has already cached its own
    instance -- the first successful completion to actually write the
    cache wins; the late arrival must never overwrite it.

    Deterministic, no sleeps: `on_second_write` (set by
    `_WriteCountingInstances`, swapped in for the registry's real
    `_instances` dict) fires only once the *second* write to that dict has
    actually executed -- i.e. exactly when the late attempt's own write
    attempt has landed, not merely when its factory function has returned.
    The final assertion only runs after that signal, so it would fail if
    `registry.py`'s cache write were ever changed from `setdefault` (write-
    if-absent) to an unconditional overwrite: `_WriteCountingInstances`
    counts either method, so the second write is still detected, and the
    assertion below would then observe the (wrong) late-arriving instance.
    """
    first_attempt_release = threading.Event()
    on_second_write = threading.Event()

    call_count_lock = threading.Lock()
    call_count = 0

    def _factory() -> _FakeEvaluator:
        nonlocal call_count
        with call_count_lock:
            this_call = call_count = call_count + 1
        if this_call == 1:
            # First attempt: block until told to finish, well after the
            # second attempt below has already completed and cached.
            first_attempt_release.wait(timeout=5.0)
        return _FakeEvaluator()

    fresh_registry = EvaluatorRegistry(
        factories={FAKE_KEY: _factory}, evaluator_init_timeout_seconds=0.05
    )
    # Swap in the instrumented cache dict before any .get() call, so both
    # the first (late) and second attempts' cache writes go through it.
    fresh_registry._instances = _WriteCountingInstances(on_second_write=on_second_write)

    with pytest.raises(EvaluatorTimeoutError):
        fresh_registry.get(*FAKE_KEY)  # first attempt times out, is abandoned

    # Second, separate attempt: constructs and caches its own instance
    # while the first is still abandoned/running in the background. This is
    # the first write to the instrumented dict.
    second_instance = fresh_registry.get(*FAKE_KEY)
    assert call_count == 2

    # Let the first (abandoned) attempt finish constructing and attempt its
    # own cache write -- the second write to the instrumented dict -- and
    # wait deterministically for exactly that write to complete before
    # asserting anything about the final cache state.
    first_attempt_release.set()
    assert on_second_write.wait(timeout=5.0), "the late attempt's cache write never landed"

    assert fresh_registry.get(*FAKE_KEY) is second_instance


def test_construction_failure_propagates_like_any_other_exception() -> None:
    """A non-timeout construction failure (e.g. no network access, a
    corrupted cache) must propagate directly from `.get()`, unwrapped --
    exactly like any other exception `execute_job` might raise -- so the
    existing, unmodified retry/dead-letter machinery in
    `worker.failure_handling` classifies and handles it with no new code."""

    def _failing_factory() -> _FakeEvaluator:
        raise RuntimeError("simulated: no network access to download the model")

    fresh_registry = EvaluatorRegistry(
        factories={FAKE_KEY: _failing_factory}, evaluator_init_timeout_seconds=5.0
    )

    with pytest.raises(RuntimeError, match="simulated: no network access"):
        fresh_registry.get(*FAKE_KEY)

    # A construction failure must never poison the cache -- a later, fixed
    # attempt (e.g. after a retry once network access returns) must be able
    # to succeed normally.
    fixed_registry = EvaluatorRegistry(
        factories={FAKE_KEY: _FakeEvaluator}, evaluator_init_timeout_seconds=5.0
    )
    assert isinstance(fixed_registry.get(*FAKE_KEY), _FakeEvaluator)
