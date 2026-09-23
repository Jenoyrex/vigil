"""Worker process entrypoint: `python -m worker`.

Wiring only -- constructs `EvaluatorRegistry` (once; see that module's own
"construct once per worker process" rationale), `Dispatcher`, and
`WorkerRuntime` from `worker.config.settings`, then calls `runtime.run()`.
No orchestration logic lives here; see `worker/runtime.py` for that.
"""

from __future__ import annotations

from worker.config import settings
from worker.dispatcher import Dispatcher
from worker.heartbeat import WATCHDOG_STALE_MULTIPLIER
from worker.logging_config import configure_logging
from worker.postgres.client import get_connection
from worker.registry import EvaluatorRegistry
from worker.resources import real_execution_resources
from worker.runtime import WorkerRuntime, generate_worker_id

# Structured (JSON Lines) logging (Phase 4D, F4) -- see
# worker/logging_config.py's module docstring. Replaces the previous
# logging.basicConfig(level=logging.INFO) plain-text setup.
configure_logging(service="worker", level=settings.log_level)


def main() -> None:
    registry = EvaluatorRegistry(
        evaluator_init_timeout_seconds=settings.evaluator_init_timeout_seconds
    )
    dispatcher = Dispatcher(
        max_concurrent_evaluations=settings.max_concurrent_evaluations,
        registry=registry,
        resource_provider=real_execution_resources,
        evaluator_call_timeout_seconds=settings.evaluator_call_timeout_seconds,
    )
    runtime = WorkerRuntime(
        dispatcher=dispatcher,
        jobs_connection_factory=get_connection,
        worker_id=generate_worker_id(),
        claim_batch_size=settings.claim_batch_size,
        poll_interval_seconds=settings.poll_interval_seconds,
        reaper_interval_seconds=settings.reaper_interval_seconds,
        stuck_job_threshold_seconds=settings.stuck_job_threshold_seconds,
        reaper_batch_size=settings.reaper_batch_size,
        max_orphaned_evaluator_threads=settings.max_orphaned_evaluator_threads,
        watchdog_stale_seconds=settings.heartbeat_stale_seconds * WATCHDOG_STALE_MULTIPLIER,
    )
    runtime.run()


if __name__ == "__main__":
    main()
