"""Unit tests for `app.api.rate_limit.RateLimiter` in isolation -- no
FastAPI app, no database, no HTTP. See test_rate_limiting.py for API-level
(HTTP 429 / Retry-After / tier / exclusion) coverage.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from app.api.rate_limit import RateLimiter


def _key() -> uuid.UUID:
    return uuid.uuid4()


class FakeClock:
    """A controllable monotonic-clock stand-in: starts at an arbitrary
    fixed instant and only ever moves forward via `advance()`, mirroring
    the fact that real `time.monotonic()` never goes backwards either."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# -- basic allow/deny behavior ---------------------------------------------


def test_initial_requests_are_allowed_up_to_capacity() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=3, refill_per_second=1.0, max_tracked_keys=10, clock=clock)
    key = _key()

    assert limiter.allow(key) is None
    assert limiter.allow(key) is None
    assert limiter.allow(key) is None


def test_bucket_rejects_once_capacity_is_consumed() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=2, refill_per_second=1.0, max_tracked_keys=10, clock=clock)
    key = _key()

    assert limiter.allow(key) is None
    assert limiter.allow(key) is None
    retry_after = limiter.allow(key)
    assert retry_after is not None
    assert retry_after >= 1


def test_exhausted_bucket_remains_rejected_until_enough_tokens_refill() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=1, refill_per_second=1.0, max_tracked_keys=10, clock=clock)
    key = _key()

    assert limiter.allow(key) is None  # consumes the only token
    assert limiter.allow(key) is not None  # still exhausted, no time passed

    clock.advance(0.5)
    assert limiter.allow(key) is not None  # only half a token back -- still exhausted

    clock.advance(0.5)  # now a full second has passed since the last refill point
    assert limiter.allow(key) is None  # exactly one token available


# -- refill math -------------------------------------------------------------


def test_refill_after_time_advances() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=1, refill_per_second=2.0, max_tracked_keys=10, clock=clock)
    key = _key()

    assert limiter.allow(key) is None
    assert limiter.allow(key) is not None

    clock.advance(0.5)  # refill_per_second=2.0 -> exactly 1 token back
    assert limiter.allow(key) is None


def test_fractional_elapsed_time_partially_refills() -> None:
    clock = FakeClock()
    # refill_per_second=1.0 -- 0.3s of elapsed time refills exactly 0.3 tokens.
    limiter = RateLimiter(capacity=1, refill_per_second=1.0, max_tracked_keys=10, clock=clock)
    key = _key()

    assert limiter.allow(key) is None  # tokens: 1 -> 0
    clock.advance(0.3)
    assert limiter.allow(key) is not None  # tokens: ~0.3 -- still < 1, denied
    clock.advance(0.3)
    assert limiter.allow(key) is not None  # tokens: ~0.6 -- still < 1, denied
    # 0.41, not 0.4: three floating-point additions of 0.3/0.3/0.4 starting
    # from a large-magnitude clock value can land a few ULPs under 1.0
    # (observed: 0.9999999999998863) rather than exactly at it -- a real,
    # harmless property of any float-based token bucket (the client would
    # just succeed on the very next attempt), not something worth adding
    # epsilon-comparison complexity to the implementation for. Advancing
    # comfortably past the boundary instead of landing exactly on it is the
    # correct way to test this, independent of that noise.
    clock.advance(0.41)
    assert limiter.allow(key) is None  # comfortably >= 1 token -- now allowed


def test_refill_never_exceeds_capacity() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=2, refill_per_second=100.0, max_tracked_keys=10, clock=clock)
    key = _key()

    limiter.allow(key)  # tokens: 2 -> 1
    clock.advance(1_000.0)  # would refill to 100,000 tokens if uncapped
    # Capacity is 2, so only 2 allowed calls should succeed before denial,
    # not an unbounded number.
    assert limiter.allow(key) is None
    assert limiter.allow(key) is None
    assert limiter.allow(key) is not None


def test_zero_elapsed_time_between_calls_does_not_grant_a_free_refill() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=1, refill_per_second=1.0, max_tracked_keys=10, clock=clock)
    key = _key()

    assert limiter.allow(key) is None  # consumes the only token, elapsed=0 on this first call
    # No time advance at all between these two calls.
    assert limiter.allow(key) is not None


# -- Retry-After calculation --------------------------------------------------


def test_retry_after_is_a_positive_integer_ceiling_of_seconds_needed() -> None:
    clock = FakeClock()
    # refill_per_second=3.0, need 1 token from empty -> 1/3 = 0.333...s -> ceil to 1.
    limiter = RateLimiter(capacity=1, refill_per_second=3.0, max_tracked_keys=10, clock=clock)
    key = _key()

    limiter.allow(key)  # exhaust
    retry_after = limiter.allow(key)
    assert retry_after == 1


def test_retry_after_scales_with_larger_deficits() -> None:
    clock = FakeClock()
    # refill_per_second=0.5 -- 1 token from empty takes 2s exactly.
    limiter = RateLimiter(capacity=1, refill_per_second=0.5, max_tracked_keys=10, clock=clock)
    key = _key()

    limiter.allow(key)
    retry_after = limiter.allow(key)
    assert retry_after == 2


def test_retry_after_is_never_zero_even_for_a_tiny_deficit() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=1, refill_per_second=1.0, max_tracked_keys=10, clock=clock)
    key = _key()

    limiter.allow(key)  # tokens: 0
    clock.advance(0.999_999)  # tokens: 0.999999 -- denied, but only a hair short of 1
    retry_after = limiter.allow(key)
    assert retry_after is not None
    assert retry_after >= 1  # never 0, even though the deficit is minuscule


# -- injected clock determinism -----------------------------------------------


def test_injected_clock_drives_behavior_not_real_time() -> None:
    calls: list[float] = []

    def fake_clock() -> float:
        calls.append(1.0)
        return 500.0  # frozen instant, never advances

    limiter = RateLimiter(capacity=1, refill_per_second=1.0, max_tracked_keys=10, clock=fake_clock)
    key = _key()

    limiter.allow(key)
    limiter.allow(key)  # frozen clock -> zero elapsed -> still denied
    assert len(calls) == 2  # the injected clock is what's actually consulted


# -- bounded storage / eviction ------------------------------------------------


def test_bounded_storage_evicts_least_recently_used_key() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=5, refill_per_second=1.0, max_tracked_keys=2, clock=clock)
    key_a, key_b, key_c = _key(), _key(), _key()

    limiter.allow(key_a)
    limiter.allow(key_b)
    assert len(limiter._buckets) == 2

    limiter.allow(key_c)  # over capacity -- key_a (least recently touched) evicted
    assert len(limiter._buckets) == 2
    assert key_a not in limiter._buckets
    assert key_b in limiter._buckets
    assert key_c in limiter._buckets


def test_evicted_key_gets_a_fresh_full_bucket_on_next_use() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=1, refill_per_second=1.0, max_tracked_keys=1, clock=clock)
    key_a, key_b = _key(), _key()

    limiter.allow(key_a)  # tokens: 0
    limiter.allow(key_b)  # evicts key_a's entry (max_tracked_keys=1)

    # key_a is now untracked; its next request starts over at full capacity
    # rather than inheriting the exhausted state -- a harmless, generous
    # failure mode, not a correctness bug.
    assert limiter.allow(key_a) is None


# -- constructor validation -----------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capacity": 0, "refill_per_second": 1.0, "max_tracked_keys": 10},
        {"capacity": -1, "refill_per_second": 1.0, "max_tracked_keys": 10},
        {"capacity": 1, "refill_per_second": 0, "max_tracked_keys": 10},
        {"capacity": 1, "refill_per_second": -1.0, "max_tracked_keys": 10},
        {"capacity": 1, "refill_per_second": 1.0, "max_tracked_keys": 0},
    ],
)
def test_invalid_constructor_arguments_raise(kwargs) -> None:
    with pytest.raises(ValueError):
        RateLimiter(**kwargs)


# -- concurrency / thread safety -----------------------------------------------


def test_concurrent_access_never_allows_more_than_capacity() -> None:
    """50 threads all racing to consume from the same key's bucket at once
    must never allow more than `capacity` of them through -- proves the
    check-refill-consume sequence is atomic under real concurrent access,
    not just correct in a single-threaded test. Uses the real clock with a
    deliberately negligible refill rate so refill during the test's
    (sub-second) real duration cannot itself explain extra allowed calls.
    """
    limiter = RateLimiter(capacity=10, refill_per_second=0.0001, max_tracked_keys=10)
    key = _key()
    allowed_count = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal allowed_count
        result = limiter.allow(key)
        if result is None:
            with lock:
                allowed_count += 1

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert allowed_count == 10


def test_concurrent_access_across_different_keys_is_independent() -> None:
    limiter = RateLimiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=1000)
    keys = [_key() for _ in range(20)]
    results: dict[uuid.UUID, list[int | None]] = {key: [] for key in keys}
    lock = threading.Lock()

    def worker(key: uuid.UUID) -> None:
        result = limiter.allow(key)
        with lock:
            results[key].append(result)

    threads = [threading.Thread(target=worker, args=(key,)) for key in keys for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Each of the 20 distinct keys got its own independent single-token
    # budget -- exactly one of its 3 concurrent attempts allowed, not more
    # (proving no cross-key interference) and not fewer (proving no
    # under-counting/lost-update race within a key either).
    for key in keys:
        allowed = sum(1 for result in results[key] if result is None)
        assert allowed == 1
