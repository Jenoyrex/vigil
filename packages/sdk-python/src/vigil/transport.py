"""HTTP delivery for `POST /v1/traces`, including bounded retry.

Retry policy: network errors, timeouts, and HTTP 429/5xx (including 503) are
retried with bounded exponential backoff, up to `max_retries` additional
attempts. 401/403/422 and any other non-retryable response are treated as
permanent and raised immediately without retrying -- resending the same
request cannot turn an auth failure or a validation error into a success.

For a 429 or 503 response carrying a `Retry-After` header (integer seconds
only -- the only form Vigil's own API ever sends), that value is used as the
retry delay verbatim, taking precedence over computed backoff and never
receiving additional jitter: a server-specified wait is already an exact
answer, not an estimate to be smoothed. Every other retryable failure falls
back to full-jitter exponential backoff (`random.uniform(0, computed_delay)`,
computed_delay bounded by `backoff_max_seconds`) so that many clients
retrying the same transient failure don't all retry in lockstep.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from vigil.exceptions import VigilDeliveryError

logger = logging.getLogger("vigil")

_TRACES_PATH = "/v1/traces"

#: Status codes for which a `Retry-After` header is inspected. Scoped to
#: exactly these two per the approved Phase 4C plan -- other 5xx responses
#: (500, 502, 504, ...) always use computed+jittered backoff, since Vigil's
#: own API only ever sends `Retry-After` on 429 (rate limiting) and 503
#: (service unavailable).
_RETRY_AFTER_STATUSES = frozenset({429, 503})


class Transport:
    def __init__(
        self,
        http_client: httpx.Client,
        *,
        max_retries: int,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        random_func: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._http = http_client
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        # Internal/testing hook, like `Vigil.__init__`'s `_transport` --
        # defaults to real `random.uniform` for full jitter; tests inject a
        # deterministic fake so delay assertions don't depend on
        # uncontrolled global randomness.
        self._random_func = random_func

    def send_batch(self, resource: dict[str, Any], spans: list[dict[str, Any]]) -> None:
        """POST one batch of `spans` (with the shared `resource`).

        Raises `VigilDeliveryError` if delivery ultimately fails. Never
        raises `httpx` exceptions directly -- every failure path (network,
        timeout, HTTP status, even an unexpected serialization error) is
        normalized to `VigilDeliveryError` so callers only need to handle
        one exception type.
        """
        body = {"resource": resource, "spans": spans}
        last_error = "unknown error"

        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            try:
                response = self._http.post(_TRACES_PATH, json=body)
            except httpx.TimeoutException as exc:
                last_error = f"timeout ({type(exc).__name__})"
            except httpx.TransportError as exc:
                last_error = f"network error ({type(exc).__name__})"
            except Exception as exc:
                # e.g. a span's input/output isn't JSON-serializable -- this
                # will fail identically on every retry, so don't retry it.
                raise VigilDeliveryError(
                    f"Failed to send {len(spans)} span(s): {type(exc).__name__}."
                ) from exc
            else:
                if response.status_code < 400:
                    return
                if not _is_retryable_status(response.status_code):
                    raise VigilDeliveryError(
                        f"Vigil API rejected {len(spans)} span(s) with "
                        f"HTTP {response.status_code}."
                    )
                last_error = f"HTTP {response.status_code}"
                if response.status_code in _RETRY_AFTER_STATUSES:
                    retry_after = _parse_retry_after(response)

            if attempt < self._max_retries:
                delay = retry_after if retry_after is not None else self._backoff_seconds(attempt)
                logger.debug(
                    "vigil: retrying span delivery (attempt %d/%d) in %.2fs after %s",
                    attempt + 1,
                    self._max_retries,
                    delay,
                    last_error,
                )
                time.sleep(delay)

        raise VigilDeliveryError(
            f"Failed to deliver {len(spans)} span(s) after "
            f"{self._max_retries + 1} attempt(s): {last_error}."
        )

    def _backoff_seconds(self, attempt: int) -> float:
        """Full-jitter exponential backoff: a random delay between 0 and the
        computed, `backoff_max_seconds`-bounded exponential value -- never
        the raw computed value itself, so concurrent clients retrying the
        same transient failure don't all wake up at once."""
        computed_delay = min(self._backoff_base * (2**attempt), self._backoff_max)
        return self._random_func(0, computed_delay)


def _is_retryable_status(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a `Retry-After` response header as integer seconds.

    Returns `None` if the header is absent or not a valid non-negative
    integer -- callers fall back to computed backoff in that case. Only the
    integer-seconds form is supported, not the HTTP-date form: RFC 7231
    permits either, but this SDK only ever talks to Vigil's own ingestion
    API, which sends (or will send) delta-seconds only -- there is no other
    Retry-After producer this SDK needs to interoperate with.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = int(value)
        if seconds < 0:
            return None
        return float(seconds)
    except (ValueError, OverflowError):
        # ValueError: not an integer at all. OverflowError: a technically
        # valid integer literal too large for `float()` to represent (e.g.
        # a malformed or malicious header with hundreds of digits) -- both
        # are "not a usable Retry-After value", not a reason to crash.
        return None
