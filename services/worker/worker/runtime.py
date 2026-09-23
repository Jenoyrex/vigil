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

**A hung `evaluate()` call can no longer wedge this runtime's shutdown**
(Phase 4A): `Dispatcher.dispatch()` -> `worker.execution.execute_job` now
bounds every evaluator call with `worker.timeouts.run_with_timeout`, so
`dispatch()` itself always returns within roughly `evaluator_call_timeout_
seconds` (or `evaluator_init_timeout_seconds` for a slow first-use
construction) of a hang, letting `run()`'s loop keep checking
`self._stop_event` and reaching a graceful shutdown promptly, exactly as it
already does for every other kind of failure.

**This is not true forced cancellation, and the underlying resource usage
is not eliminated.** Python cannot forcibly stop a running thread -- a
timed-out call is *abandoned*, not stopped; its thread may keep running,
unsupervised, for however long it takes to finish on its own or the
process exits (see `worker/timeouts.py`'s module docstring for exactly
why, verified against this project's own installed CPython). What Phase 4A
adds instead is a bound on the *consequence*: `run()`'s loop also checks
`worker.timeouts.outstanding_orphaned_calls()` every tick, and once that
count reaches `max_orphaned_evaluator_threads`, this runtime calls its own
`request_stop()` -- the exact same graceful-shutdown path a SIGTERM already
triggers -- rather than letting the count grow without limit. Recovery
after that depends entirely on an external process supervisor
(systemd/Docker/Kubernetes restart policy) bringing up a fresh,
zero-orphan replacement; this runtime only ever decides when to retire
itself, never how it comes back. `stuck_job_threshold_seconds`/the reaper
remain the separate, complementary backstop for the case neither timeout
nor this threshold can address at all: a worker process that has died
outright and can never itself decide anything.
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
from worker.heartbeat import HeartbeatWatchdog, touch_heartbeat
from worker.postgres.repository import ClaimedJob, EvaluationJobsRepository
from worker.reaper import ReapedJobOutcome, reap_stuck_jobs
from worker.timeouts import outstanding_orphaned_calls

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
    timing/batch settings); `run()` loops until a shutdown signal -- external
    (SIGTERM/SIGINT) or self-triggered (the orphaned-evaluator-call
    threshold below) -- is received. See module docstring for exactly what
    Phase 4A's timeout/self-restart mechanism does and does not guarantee.
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
        max_orphaned_evaluator_threads: int = 4,
        heartbeat_callback: Callable[[], None] = touch_heartbeat,
        watchdog_stale_seconds: float | None = None,
    ) -> None:
        self._watchdog_stale_seconds = watchdog_stale_seconds
        self._dispatcher = dispatcher
        self._jobs_connection_factory = jobs_connection_factory
        self._worker_id = worker_id
        self._claim_batch_size = claim_batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._reaper_interval_seconds = reaper_interval_seconds
        self._stuck_job_threshold_seconds = stuck_job_threshold_seconds
        self._reaper_batch_size = reaper_batch_size
        self._max_orphaned_evaluator_threads = max_orphaned_evaluator_threads
        self._heartbeat_callback = heartbeat_callback
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
        check the orphaned-evaluator-call threshold, maybe-reap (tracked via
        `time.monotonic()`, never wall-clock time, so a system clock
        adjustment can't skew the interval), and an interruptible idle wait
        when nothing was claimed. `self._stop_event` is checked between
        every phase so a shutdown request -- external or self-triggered --
        never triggers one more unit of unnecessary work.

        `self._heartbeat_callback()` (Phase 4D, F6 -- `worker.heartbeat
        .touch_heartbeat` by default) is called once immediately at
        startup and once at the top of every loop iteration thereafter --
        never inside a phase itself, so its own cost can never be what
        blocks a shutdown check. Every phase called between one heartbeat
        and the next is now individually time-bounded (PostgreSQL via
        `database_timeout_seconds`, ClickHouse via
        `clickhouse_timeout_seconds`, one evaluator call via
        `evaluator_call_timeout_seconds`/`evaluator_init_timeout_seconds`),
        so a heartbeat that stops refreshing for longer than a generous
        multiple of those bounds is a genuine "this loop is stuck, not just
        busy" signal -- see `services/worker/Dockerfile`'s `HEALTHCHECK`
        instruction, which is what actually consumes this.
        """
        self._install_signal_handlers()
        logger.info(
            "worker runtime starting worker_id=%s",
            self._worker_id,
            extra={"worker_id": self._worker_id},
        )
        self._heartbeat_callback()
        if self._watchdog_stale_seconds is not None:
            # Force-exits this process if the loop below ever stops
            # heartbeating, so `restart: unless-stopped` recovers it -- see
            # `worker.heartbeat.HeartbeatWatchdog`. Started only after the
            # first heartbeat, so startup is never seen as stale.
            HeartbeatWatchdog(
                stale_seconds=self._watchdog_stale_seconds,
                stop_event=self._stop_event,
                service="worker",
            ).start()

        self._reap()
        last_reap_at = time.monotonic()

        while not self._stop_event.is_set():
            self._heartbeat_callback()

            did_work = self._claim_and_dispatch()
            if self._stop_event.is_set():
                break

            self._check_orphaned_evaluator_threshold()
            if self._stop_event.is_set():
                break

            if time.monotonic() - last_reap_at >= self._reaper_interval_seconds:
                self._reap()
                last_reap_at = time.monotonic()
            if self._stop_event.is_set():
                break

            if not did_work:
                self._stop_event.wait(self._poll_interval_seconds)

        logger.info(
            "worker runtime stopped worker_id=%s",
            self._worker_id,
            extra={"worker_id": self._worker_id},
        )

    def _check_orphaned_evaluator_threshold(self) -> None:
        """Self-triggers the exact same graceful shutdown a SIGTERM already
        does (`request_stop()`) once `worker.timeouts.outstanding_orphaned_calls()`
        reaches `self._max_orphaned_evaluator_threads` -- see module
        docstring for what this does and does not guarantee. Never itself
        raises; reading the counter is a plain, lock-guarded integer read
        with no I/O."""
        outstanding = outstanding_orphaned_calls()
        if outstanding >= self._max_orphaned_evaluator_threads:
            logger.error(
                "worker runtime worker_id=%s: %d hung evaluator call(s) outstanding "
                "(limit %d) -- requesting self-restart",
                self._worker_id,
                outstanding,
                self._max_orphaned_evaluator_threads,
                extra={
                    "worker_id": self._worker_id,
                    "outstanding": outstanding,
                    "limit": self._max_orphaned_evaluator_threads,
                },
            )
            self.request_stop()

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object) -> None:
        logger.info(
            "worker runtime received signal %s, requesting shutdown",
            signum,
            extra={"worker_id": self._worker_id, "signal": signum},
        )
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
            logger.exception(
                "claim tick failed; treating as idle", extra={"worker_id": self._worker_id}
            )
            return False

        if not claimed:
            return False

        outcomes = self._dispatcher.dispatch(claimed)
        _log_dispatch_outcomes(self._worker_id, claimed, outcomes)
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
            logger.exception("reap tick failed", extra={"worker_id": self._worker_id})
            return

        _log_reap_outcomes(self._worker_id, outcomes)


def _log_dispatch_outcomes(
    worker_id: str, claimed: list[ClaimedJob], outcomes: list[DispatchOutcome]
) -> None:
    """One aggregated line per non-empty dispatch tick -- counts, not one
    line per job, so a busy worker's log doesn't scale linearly with job
    volume. Job-level detail (job_id, project_id, evaluator) for any
    individual failure is logged separately, at the point it happened --
    see worker.dispatcher.Dispatcher._handle_failure."""
    statuses = Counter(
        "succeeded" if outcome.succeeded else type(outcome.error).__name__ for outcome in outcomes
    )
    logger.info(
        "dispatch tick: claimed=%d outcomes=%s",
        len(claimed),
        dict(statuses),
        extra={"worker_id": worker_id, "claimed": len(claimed), "outcomes": dict(statuses)},
    )


def _log_reap_outcomes(worker_id: str, outcomes: list[ReapedJobOutcome]) -> None:
    if not outcomes:
        return
    statuses = Counter(outcome.new_status if outcome.recorded else "stale" for outcome in outcomes)
    logger.info(
        "reap tick: reclaimed=%d outcomes=%s",
        len(outcomes),
        dict(statuses),
        extra={"worker_id": worker_id, "reclaimed": len(outcomes), "outcomes": dict(statuses)},
    )
