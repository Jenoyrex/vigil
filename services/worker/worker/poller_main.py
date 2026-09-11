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

import logging

from worker.clickhouse.client import get_clickhouse_client
from worker.clickhouse.eligible_span_repository import EligibleSpanRepository
from worker.config import settings
from worker.poller import HttpJobCreationClient, Poller
from worker.postgres.client import get_connection
from worker.registry import EvaluatorRegistry

logging.basicConfig(level=logging.INFO)


def main() -> None:
    registry = EvaluatorRegistry()
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
    )
    poller.run()


if __name__ == "__main__":
    main()
