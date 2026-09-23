"""Evaluation job poller process entrypoint -- Phase 3H.

Wiring only, mirroring `worker/__main__.py`'s exact style: constructs
`EvaluatorRegistry` (once), `EligibleSpanRepository`, `HttpJobCreationClient`,
and `Poller` from `worker.config.settings`, then calls `poller.run()`. No
orchestration logic lives here; see `worker/poller.py` for that.

Runs as a fully independent process from `python -m worker`
(`worker/__main__.py` / `worker.runtime.WorkerRuntime`) -- neither imports
the other, and this module never touches `worker/runtime.py`.

Callable directly (`python -m worker.poller_main`) or, equivalently, via
`python -m worker.poller` (that module's own `if __name__ == "__main__"`
delegates to this one's `main()`).
"""

from __future__ import annotations

from worker.clickhouse.client import get_clickhouse_client
from worker.clickhouse.eligible_span_repository import EligibleSpanRepository
from worker.config import settings
from worker.heartbeat import WATCHDOG_STALE_MULTIPLIER
from worker.logging_config import configure_logging
from worker.poller import HttpJobCreationClient, Poller
from worker.postgres.client import get_connection
from worker.registry import EvaluatorRegistry

# Structured (JSON Lines) logging (Phase 4D, F4) -- see
# worker/logging_config.py's module docstring. `service="poller"`
# (distinct from worker/__main__.py's "worker") so a JSON log consumer can
# tell the two processes apart even though they share one image/package.
configure_logging(service="poller", level=settings.log_level)


def main() -> None:
    # `evaluator_init_timeout_seconds` is threaded through for consistency
    # with `worker/__main__.py`'s identical construction, but is never
    # actually exercised by this process: `worker.poller.Poller` only ever
    # calls `registry.registered_keys()` (never `.get()`), so no evaluator
    # is ever lazily constructed here -- see worker/registry.py's
    # `registered_keys()` docstring.
    registry = EvaluatorRegistry(
        evaluator_init_timeout_seconds=settings.evaluator_init_timeout_seconds
    )
    eligible_span_repository = EligibleSpanRepository(get_clickhouse_client())
    job_creation_client = HttpJobCreationClient(
        base_url=settings.api_base_url,
        internal_service_token=settings.internal_service_token,
        timeout_seconds=settings.poller_job_creation_timeout_seconds,
    )
    poller = Poller(
        eligible_span_repository=eligible_span_repository,
        checkpoint_connection_factory=get_connection,
        job_creation_client=job_creation_client,
        registry=registry,
        poller_batch_size=settings.poller_batch_size,
        poller_overlap_seconds=settings.poller_overlap_seconds,
        poller_start_time_lookback_days=settings.poller_start_time_lookback_days,
        poller_interval_seconds=settings.poller_interval_seconds,
        watchdog_stale_seconds=settings.heartbeat_stale_seconds * WATCHDOG_STALE_MULTIPLIER,
    )
    poller.run()


if __name__ == "__main__":
    main()
