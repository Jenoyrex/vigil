"""Tests for worker.timeouts.run_with_timeout / outstanding_orphaned_calls.

No real infinite hangs anywhere -- every "slow" callable blocks on a
`threading.Event` the test itself releases, and `_poll_until` bounds every
wait for a background thread's own completion.
"""

from __future__ import annotations

import threading
import time

import pytest

from worker.timeouts import EvaluatorTimeoutError, outstanding_orphaned_calls, run_with_timeout


def _poll_until(predicate, *, timeout_seconds: float = 2.0, interval_seconds: float = 0.01) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_seconds)
    raise AssertionError(f"condition not met within {timeout_seconds}s")


def test_returns_the_result_when_the_callable_finishes_in_time() -> None:
    assert run_with_timeout(lambda: 42, timeout_seconds=1.0) == 42


def test_reraises_the_callables_own_exception_when_it_finishes_in_time() -> None:
    def _raises() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_with_timeout(_raises, timeout_seconds=1.0)


def test_raises_evaluator_timeout_error_when_the_callable_is_too_slow() -> None:
    release = threading.Event()

    def _slow() -> None:
        release.wait(timeout=5.0)

    with pytest.raises(EvaluatorTimeoutError, match=r"did not complete within 0\.05s"):
        run_with_timeout(_slow, timeout_seconds=0.05)

    release.set()  # let the orphaned thread finish so it doesn't outlive this test


def test_timeout_error_message_never_includes_the_callables_own_data() -> None:
    """The message must carry only the configured timeout value -- never
    any argument/closure content the wrapped callable happens to use."""
    secret_payload = "sk-super-secret-input-text-do-not-log-me"
    release = threading.Event()

    def _slow_with_captured_data() -> str:
        release.wait(timeout=5.0)
        return secret_payload

    with pytest.raises(EvaluatorTimeoutError) as excinfo:
        run_with_timeout(_slow_with_captured_data, timeout_seconds=0.05)

    release.set()
    assert secret_payload not in str(excinfo.value)


def test_the_abandoned_thread_is_a_daemon_thread() -> None:
    release = threading.Event()
    seen_thread: list[threading.Thread] = []

    def _slow() -> None:
        seen_thread.append(threading.current_thread())
        release.wait(timeout=5.0)

    with pytest.raises(EvaluatorTimeoutError):
        run_with_timeout(_slow, timeout_seconds=0.05)

    _poll_until(lambda: len(seen_thread) == 1)
    assert seen_thread[0].daemon is True

    release.set()


def test_orphan_counter_increments_on_timeout_and_decrements_after_the_abandoned_call_exits() -> (
    None
):
    baseline = outstanding_orphaned_calls()
    release = threading.Event()

    def _slow() -> None:
        release.wait(timeout=5.0)

    with pytest.raises(EvaluatorTimeoutError):
        run_with_timeout(_slow, timeout_seconds=0.05)

    assert outstanding_orphaned_calls() == baseline + 1

    release.set()
    _poll_until(lambda: outstanding_orphaned_calls() == baseline)


def test_orphan_counter_does_not_increment_when_the_callable_finishes_in_time() -> None:
    baseline = outstanding_orphaned_calls()
    run_with_timeout(lambda: None, timeout_seconds=1.0)
    assert outstanding_orphaned_calls() == baseline


def test_orphan_counter_cannot_double_decrement() -> None:
    """Each timed-out call spawns exactly one watcher, which joins its own
    thread and decrements exactly once -- three independent timeouts must
    net to exactly three decrements, never more, once all three abandoned
    calls finish."""
    baseline = outstanding_orphaned_calls()
    releases = [threading.Event() for _ in range(3)]

    def _make_slow(release: threading.Event):
        def _slow() -> None:
            release.wait(timeout=5.0)

        return _slow

    for release in releases:
        with pytest.raises(EvaluatorTimeoutError):
            run_with_timeout(_make_slow(release), timeout_seconds=0.05)

    assert outstanding_orphaned_calls() == baseline + 3

    for release in releases:
        release.set()

    _poll_until(lambda: outstanding_orphaned_calls() == baseline)
    # Give any stray extra decrement a chance to fire before asserting it
    # never goes negative relative to baseline.
    time.sleep(0.1)
    assert outstanding_orphaned_calls() == baseline
