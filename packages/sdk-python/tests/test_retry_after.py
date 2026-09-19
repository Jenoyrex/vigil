"""Unit-level tests for `vigil.transport`'s `Retry-After` parsing and
full-jitter backoff, isolated from the full `Vigil`/`vigil_factory` stack
(see test_retries.py for end-to-end retry-and-succeed/exhaustion coverage).
"""

from __future__ import annotations

import httpx
import pytest

from vigil.transport import Transport, _is_retryable_status, _parse_retry_after


def _make_transport(*, random_func=None, backoff_base=1.0, backoff_max=8.0) -> Transport:
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    kwargs = {} if random_func is None else {"random_func": random_func}
    return Transport(
        http_client,
        max_retries=3,
        backoff_base_seconds=backoff_base,
        backoff_max_seconds=backoff_max,
        **kwargs,
    )


# -- status classification -----------------------------------------------


def test_429_is_retryable() -> None:
    assert _is_retryable_status(429) is True


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_5xx_is_retryable(status_code: int) -> None:
    assert _is_retryable_status(status_code) is True


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422])
def test_ordinary_4xx_is_not_retryable(status_code: int) -> None:
    assert _is_retryable_status(status_code) is False


# -- Retry-After parsing ---------------------------------------------------


def test_parse_retry_after_valid_integer_seconds() -> None:
    response = httpx.Response(429, headers={"Retry-After": "5"})
    assert _parse_retry_after(response) == 5.0


def test_parse_retry_after_zero_is_valid() -> None:
    response = httpx.Response(429, headers={"Retry-After": "0"})
    assert _parse_retry_after(response) == 0.0


def test_parse_retry_after_missing_header_is_none() -> None:
    response = httpx.Response(429)
    assert _parse_retry_after(response) is None


def test_parse_retry_after_negative_is_none() -> None:
    response = httpx.Response(429, headers={"Retry-After": "-5"})
    assert _parse_retry_after(response) is None


def test_parse_retry_after_non_integer_is_none() -> None:
    response = httpx.Response(429, headers={"Retry-After": "soon"})
    assert _parse_retry_after(response) is None


def test_parse_retry_after_absurdly_large_integer_is_none() -> None:
    # A technically-valid integer literal (Python ints are unbounded) but
    # too large for float() to represent -- float(int("9" * 400)) raises
    # OverflowError, which must be treated the same as any other malformed
    # value (fall back to computed backoff), not left to crash send_batch.
    response = httpx.Response(429, headers={"Retry-After": "9" * 400})
    assert _parse_retry_after(response) is None


def test_parse_retry_after_http_date_form_is_none() -> None:
    # RFC 7231 permits an HTTP-date form too; deliberately unsupported here
    # (see _parse_retry_after's docstring) -- falls back to computed backoff
    # exactly like any other malformed value.
    response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert _parse_retry_after(response) is None


# -- full-jitter backoff ---------------------------------------------------


def test_backoff_seconds_uses_injected_random_func_as_full_jitter() -> None:
    captured: list[tuple[float, float]] = []

    def fake_random(low: float, high: float) -> float:
        captured.append((low, high))
        return high  # deterministic: always the upper bound

    transport = _make_transport(random_func=fake_random, backoff_base=1.0, backoff_max=8.0)
    delay = transport._backoff_seconds(attempt=2)  # computed = min(1 * 2**2, 8) = 4.0

    assert captured == [(0, 4.0)]
    assert delay == 4.0


def test_backoff_seconds_computed_delay_is_bounded_by_backoff_max() -> None:
    captured: list[tuple[float, float]] = []

    def fake_random(low: float, high: float) -> float:
        captured.append((low, high))
        return high

    transport = _make_transport(random_func=fake_random, backoff_base=1.0, backoff_max=8.0)
    # 1 * 2**10 == 1024, far above backoff_max -- must be capped to 8.0
    # before being passed to the jitter function at all.
    transport._backoff_seconds(attempt=10)

    assert captured == [(0, 8.0)]


def test_backoff_seconds_default_random_func_stays_within_bounds() -> None:
    # No injected random_func here -- exercises the real `random.uniform`
    # default end-to-end, over several attempts, asserting only the
    # documented range (an exact-value assertion against real randomness
    # would be flaky by construction).
    transport = _make_transport(backoff_base=1.0, backoff_max=8.0)
    for attempt in range(6):
        computed_delay = min(1.0 * 2**attempt, 8.0)
        delay = transport._backoff_seconds(attempt)
        assert 0.0 <= delay <= computed_delay
