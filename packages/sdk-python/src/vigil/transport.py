"""HTTP delivery for `POST /v1/traces`, including bounded retry.

Retry policy: network errors, timeouts, and HTTP 5xx (including 503) are
retried with bounded exponential backoff, up to `max_retries` additional
attempts. 401/403/422 and any other non-5xx response are treated as
permanent and raised immediately without retrying -- resending the same
request cannot turn an auth failure or a validation error into a success.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from vigil.exceptions import VigilDeliveryError

logger = logging.getLogger("vigil")

_TRACES_PATH = "/v1/traces"


class Transport:
    def __init__(
        self,
        http_client: httpx.Client,
        *,
        max_retries: int,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
    ) -> None:
        self._http = http_client
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds

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

            if attempt < self._max_retries:
                delay = self._backoff_seconds(attempt)
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
        return min(self._backoff_base * (2**attempt), self._backoff_max)


def _is_retryable_status(status_code: int) -> bool:
    return status_code >= 500
