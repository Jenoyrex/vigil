"""Per-API-key in-process token-bucket rate limiting (Phase 4C).

Two independent tiers -- `get_ingestion_rate_limiter` (stricter, for
`POST /v1/traces`) and `get_default_rate_limiter` (more generous, shared by
every other authenticated customer endpoint) -- each keyed by
`AuthenticatedKey.api_key_id`, never the raw API key and never `project_id`
alone: `api_key_id` is the actual caller identity `app.api.deps.
get_current_api_key` already resolves server-side, and one project can hold
several keys that should not share a budget.

In-process only, by design: `docs/decisions/006-deployment-architecture.md`
decision 4 already rejected introducing new infrastructure (Redis
specifically named) without a driving requirement, and today's production
topology (`infrastructure/docker-compose.prod.yml`) runs exactly one `api`
replica. Each `api` process enforces its own independent limit for now --
horizontally scaling `api` to multiple replicas would need revisiting this
design (a shared store), exactly the kind of scoped-for-now decision ADR 006
decision 7 already made for Alembic migrations.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status

from app.api.deps import AuthenticatedKey, get_current_api_key
from app.config import settings

RETRY_AFTER_HEADER = "Retry-After"


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """A per-key token bucket.

    Bounded to at most `max_tracked_keys` concurrently-tracked keys
    (least-recently-used evicted first) so an arbitrary number of distinct
    API keys cannot grow this process's memory without bound -- eviction
    simply resets that key to a fresh, full bucket on its next request,
    which is a harmless, generous failure mode, not a correctness bug.

    Thread-safe: FastAPI runs a synchronous `def` route (every route in
    this API) and its dependencies in a worker thread pool, so concurrent
    requests genuinely call `allow()` from multiple OS threads at once, not
    just multiple async tasks on one thread.
    """

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_second: float,
        max_tracked_keys: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive.")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive.")
        if max_tracked_keys <= 0:
            raise ValueError("max_tracked_keys must be positive.")
        self._capacity = float(capacity)
        self._refill_per_second = float(refill_per_second)
        self._max_tracked_keys = max_tracked_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: OrderedDict[uuid.UUID, _Bucket] = OrderedDict()

    def allow(self, key: uuid.UUID) -> int | None:
        """Attempt to consume one token for `key`.

        Returns `None` if allowed (a token was consumed). Returns the
        number of whole seconds to wait before retrying if not allowed (no
        token is consumed in that case) -- always a positive integer, never
        `0`, so a denied caller is never told to retry immediately when it
        would just be denied again.
        """
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, last_refill=now)
                self._buckets[key] = bucket
                self._evict_oldest_if_over_capacity()
            else:
                self._buckets.move_to_end(key)

            # Clamp negative elapsed time to zero rather than letting it
            # reduce `tokens` -- `self._clock` is monotonic in production
            # (never goes backwards), but a test-injected clock or an
            # unforeseen clock edge case must never be able to charge a
            # caller extra tokens for time that didn't pass.
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(
                self._capacity, bucket.tokens + elapsed * self._refill_per_second
            )
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return None

            deficit = 1.0 - bucket.tokens
            seconds_needed = deficit / self._refill_per_second
            return max(1, math.ceil(seconds_needed))

    def _evict_oldest_if_over_capacity(self) -> None:
        while len(self._buckets) > self._max_tracked_keys:
            self._buckets.popitem(last=False)


_ingestion_limiter = RateLimiter(
    capacity=settings.rate_limit_ingestion_capacity,
    refill_per_second=settings.rate_limit_ingestion_refill_per_second,
    max_tracked_keys=settings.rate_limit_max_tracked_api_keys,
)
_default_limiter = RateLimiter(
    capacity=settings.rate_limit_default_capacity,
    refill_per_second=settings.rate_limit_default_refill_per_second,
    max_tracked_keys=settings.rate_limit_max_tracked_api_keys,
)


def get_ingestion_rate_limiter() -> RateLimiter:
    return _ingestion_limiter


def get_default_rate_limiter() -> RateLimiter:
    return _default_limiter


def _enforce(auth: AuthenticatedKey, limiter: RateLimiter) -> AuthenticatedKey:
    """Shared by both tiers below. Only ever called after `auth` has
    already been resolved by `get_current_api_key` -- FastAPI resolves a
    dependency's own sub-dependencies (here, `get_current_api_key`) before
    calling it, so an unauthenticated or invalid-key request is already
    rejected with 401 before this function, or `RateLimiter.allow`, ever
    runs. No unauthenticated caller can consume, probe, or discover another
    customer's bucket."""
    retry_after_seconds = limiter.allow(auth.api_key_id)
    if retry_after_seconds is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Retry after {retry_after_seconds} seconds.",
            headers={RETRY_AFTER_HEADER: str(retry_after_seconds)},
        )
    return auth


def require_ingestion_rate_limit(
    auth: AuthenticatedKey = Depends(get_current_api_key),
    limiter: RateLimiter = Depends(get_ingestion_rate_limiter),
) -> AuthenticatedKey:
    """Drop-in replacement for `Depends(get_current_api_key)` on
    `POST /v1/traces`: authenticates exactly as before, then additionally
    enforces the stricter ingestion rate limit. FastAPI caches
    `get_current_api_key`'s result per request, so routes needing both the
    resolved `AuthenticatedKey` and rate limiting only get one DB lookup."""
    return _enforce(auth, limiter)


def require_default_rate_limit(
    auth: AuthenticatedKey = Depends(get_current_api_key),
    limiter: RateLimiter = Depends(get_default_rate_limiter),
) -> AuthenticatedKey:
    """Drop-in replacement for `Depends(get_current_api_key)` on every
    authenticated customer endpoint other than `POST /v1/traces`. See
    `require_ingestion_rate_limit`."""
    return _enforce(auth, limiter)
