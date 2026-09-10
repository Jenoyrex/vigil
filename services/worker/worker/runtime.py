"""Worker process runtime: turns the already-built claim/dispatch/reap
mechanics (`worker.postgres.repository.EvaluationJobsRepository.claim_jobs`,
`worker.dispatcher.Dispatcher`, `worker.reaper.reap_stuck_jobs`) into a
continuously-running loop. `WorkerRuntime` is orchestration only -- it
decides *when* to claim, dispatch, and reap, never *how*; none of those three
mechanisms are modified by this module.

Deliberately not ADR 005 section 7's "Poller" -- that (still unbuilt, blocked
on `apps/api`'s job-creation endpoint) scans ClickHouse `spans` and *creates*
`evaluation_jobs` rows. This module operates on rows that already exist:
claim them, run them, and reclaim the ones a crashed worker abandoned.

Dependency injection throughout, mirroring `worker.dispatcher.Dispatcher`'s
own style: `WorkerRuntime` is constructed with an already-built `Dispatcher`
(it never constructs `EvaluatorRegistry` or `Dispatcher` itself -- that is
`worker/__main__.py`'s job) and a `jobs_connection_factory` (mirroring
`Dispatcher`'s own `ResourceProvider` injection point), so tests can supply
fakes for both without touching a real database or a real evaluator.

**Known limitation, not addressed here**: `Dispatcher.dispatch()` waits for
every submitted evaluation to complete (`[future.result() for future in
futures]` inside a `ThreadPoolExecutor` context manager) before returning.
There is no per-call evaluator timeout anywhere in this codebase yet
(`worker/config.py`'s own docstring already flags `evaluator_call_timeout_seconds`
as planned but unimplemented). If a single `evaluate()` call hangs forever,
`dispatch()` never returns, and this runtime's claim/dispatch tick blocks
indefinitely -- including its ability to notice `self._stop_event` and shut
down gracefully. `stuck_job_threshold_seconds` mitigates the *database*
consequence (another worker process, or this one after an eventual restart,
reclaims the row), but does nothing for *this* process's own liveness or
shutdown responsiveness while stuck. Closing this gap requires per-call
timeout enforcement inside `Dispatcher`/`worker/execution.py` -- a future
phase, deliberately out of scope here, exactly as it was excluded from
Phase 3F's stuck-job reaper.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable

import psycopg

from worker.dispatcher import Dispatcher, DispatchOutcome
from worker.postgres.repository import ClaimedJob, EvaluationJobsRepository
from worker.reaper import ReapedJobOutcome, reap_stuck_jobs

logger = logging.getLogger(__name__)

#: A fresh `psycopg.Connection` per call -- `worker.postgres.client.get_connection`'s
#: exact shape, injected rather than imported directly so tests can supply a
#: factory that returns a fake connection instead of opening a real one.
JobsConnectionFactory = Callable[[], psycopg.Connection]


def generate_worker_id() -> str:
    """`hostname:pid:short-uuid`, generated once per process. Call this
    exactly once at process startup (`worker/__main__.py`) and reuse the
    result for every `claim_jobs` call that process makes -- it becomes
    `evaluation_jobs.claimed_by`, which `worker.reaper`'s diagnostic
    messages already surface for stuck-job forensics.
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class WorkerRuntime:
    """Constructed once per process with everything it needs already built
    (`dispatcher`) or injectable (`jobs_connection_factory`, `worker_id`,
    timing/batch settings); `run()` loops until a shutdown signal is
    received. See module docstring for the one significant limitation this
    phase does not address.
    """

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        jobs_connection_factory: JobsConnectionFactory,
        worker_id: str,
        claim_batch_size: int,
        poll_interval_seconds: float,
        reaper_interval_seconds: float,
        stuck_job_threshold_seconds: float,
        reaper_batch_size: int,
    ) -> None:
        self._dispatcher = dispatcher
        self._jobs_connection_factory = jobs_connection_factory
        self._worker_id = worker_id
        self._claim_batch_size = claim_batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._reaper_interval_seconds = reaper_interval_seconds
        self._stuck_job_threshold_seconds = stuck_job_threshold_seconds
        self._reaper_batch_size = reaper_batch_size
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Signal-handler-safe: only ever sets an already-constructed
        `threading.Event`, never touches a database connection or does any
        other work. See `_install_signal_handlers`.
        """
        self._stop_event.set()

    def run(self) -> None:
        """Must be called from the process's main thread -- `signal.signal`
        (used by `_install_signal_handlers`) only works from the main
        thread in Python; calling `run()` from any other thread would raise
        when installing the handlers.

        Reaps once at startup (safe: idempotent, fenced -- see
        `worker.reaper`'s own concurrency guarantees, which do not depend on
        this being the only reaper running), then loops: claim-and-dispatch,
        maybe-reap (tracked via `time.monotonic()`, never wall-clock time,
        so a system clock adjustment can't skew the interval), and an
        interruptible idle wait when nothing was claimed. `self._stop_event`
        is checked between every phase so a shutdown request never triggers
        one more unit of unnecessary work.
        """
        self._install_signal_handlers()
        logger.info("worker runtime starting worker_id=%s", self._worker_id)

        self._reap()
        last_reap_at = time.monotonic()

        while not self._stop_event.is_set():
            did_work = self._claim_and_dispatch()
            if self._stop_event.is_set():
                break

            if time.monotonic() - last_reap_at >= self._reaper_interval_seconds:
                self._reap()
                last_reap_at = time.monotonic()
            if self._stop_event.is_set():
                break

            if not did_work:
                self._stop_event.wait(self._poll_interval_seconds)

        logger.info("worker runtime stopped worker_id=%s", self._worker_id)

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object) -> None:
        logger.info("worker runtime received signal %s, requesting shutdown", signum)
        self.request_stop()

    def _claim_and_dispatch(self) -> bool:
        """One claim tick. Returns `True` if any job was claimed (the loop
        should not sleep before trying again -- there may be more queued
        work), `False` if the claim was empty or the claim itself failed.
        Never raises: a claim/DB exception is logged and treated the same
        as an empty claim, so a transient outage backs off via the normal
        idle-sleep path rather than crashing the runtime.
        """
        try:
            connection = self._jobs_connection_factory()
            try:
                claimed = EvaluationJobsRepository(connection).claim_jobs(
                    worker_id=self._worker_id, batch_size=self._claim_batch_size
                )
            finally:
                connection.close()
        except Exception:
            logger.exception("claim tick failed; treating as idle")
            return False

        if not claimed:
            return False

        outcomes = self._dispatcher.dispatch(claimed)
        _log_dispatch_outcomes(claimed, outcomes)
        return True

    def _reap(self) -> None:
        """One reap tick. Never raises: a reap/DB exception is logged, and
        the stuck jobs it would have reclaimed simply remain `running`
        until the next reap tick (or another worker process's).
        """
        try:
            connection = self._jobs_connection_factory()
            try:
                outcomes = reap_stuck_jobs(
                    jobs_repository=EvaluationJobsRepository(connection),
                    stuck_threshold_seconds=self._stuck_job_threshold_seconds,
                    batch_size=self._reaper_batch_size,
                )
            finally:
                connection.close()
        except Exception:
            logger.exception("reap tick failed")
            return

        _log_reap_outcomes(outcomes)


def _log_dispatch_outcomes(claimed: list[ClaimedJob], outcomes: list[DispatchOutcome]) -> None:
    """One aggregated line per non-empty dispatch tick -- counts, not one
    line per job, so a busy worker's log doesn't scale linearly with job
    volume."""
    statuses = Counter(
        "succeeded" if outcome.succeeded else type(outcome.error).__name__ for outcome in outcomes
    )
    logger.info("dispatch tick: claimed=%d outcomes=%s", len(claimed), dict(statuses))


def _log_reap_outcomes(outcomes: list[ReapedJobOutcome]) -> None:
    if not outcomes:
        return
    statuses = Counter(outcome.new_status if outcome.recorded else "stale" for outcome in outcomes)
    logger.info("reap tick: reclaimed=%d outcomes=%s", len(outcomes), dict(statuses))
