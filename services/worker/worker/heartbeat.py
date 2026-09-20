"""Liveness heartbeat for `WorkerRuntime.run()` and `Poller.run()`
(Phase 4D, F6) -- a file whose mtime is refreshed once per completed loop
iteration, checked by `services/worker/Dockerfile`'s `HEALTHCHECK`
instruction.

Deliberately not an HTTP endpoint: neither process runs a server (see
`services/worker/Dockerfile`'s own comment), and adding one solely to
satisfy a healthcheck would be new exposed network surface for zero other
benefit. A heartbeat file needs no listener and no port, and -- because it
is written from inside the exact loop it certifies, once per iteration --
a genuinely stuck loop (one that never completes another iteration) simply
stops updating it. That is the entire point: this must distinguish "the
process is running" (all Docker's default liveness, and everything this
image had before this, could ever say) from "the process is actually
making progress."

Phase 4D's F5 fix (worker/postgres/client.py's `connect_timeout`/
`statement_timeout`) is what makes this heartbeat meaningful rather than
cosmetic: every I/O call inside one loop iteration -- PostgreSQL (F5),
ClickHouse (`clickhouse_timeout_seconds`, pre-existing), the evaluator call
itself (`evaluator_call_timeout_seconds`/`evaluator_init_timeout_seconds`,
Phase 4A), and the poller's HTTP call to apps/api
(`poller_job_creation_timeout_seconds`, pre-existing) -- now has *some*
upper bound, so one iteration completing (and this heartbeat refreshing)
within a generous, bounded window is a real signal, not a guess.

`HEARTBEAT_PATH` is under `/tmp` deliberately: `/tmp` is world-writable
(mode 1777) in the `python:3.12-slim-bookworm` base image by design, so the
non-root `vigil` user (`services/worker/Dockerfile`) can write here with no
additional `chown`/directory-creation step -- unlike the FastEmbed cache
directory that same Dockerfile's own Phase 4C review had to fix explicitly
for exactly the opposite reason. This exact literal path is duplicated
(not imported -- a `HEALTHCHECK CMD` runs as its own, separate process) into
that Dockerfile's `HEALTHCHECK` instruction; if this constant ever changes,
that instruction must change with it.

Phase 4D's stuck-container recovery: Docker's `HEALTHCHECK` only *labels* a
container unhealthy -- it never restarts it, and the reaper runs inside the
very loop that would be stuck. `HeartbeatWatchdog` (below) closes that: a
daemon thread that force-exits the process when the heartbeat goes stale, so
`restart: unless-stopped` recovers the container.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("/tmp/vigil-worker-heartbeat")

#: Exit status of a watchdog-forced exit (`EX_SOFTWARE`); any non-zero exit makes
#: `restart: unless-stopped` restart the container.
WATCHDOG_EXIT_CODE = 70

#: The watchdog fires at this multiple of `heartbeat_stale_seconds`, i.e. well
#: after Docker's `HEALTHCHECK` has already reported unhealthy, so a merely busy
#: iteration (a large claim batch, a cold model load) is never killed by it.
WATCHDOG_STALE_MULTIPLIER = 2.0

_last_beat = time.monotonic()


def touch_heartbeat() -> None:
    """Refresh the heartbeat file's mtime (creating it on first call), and the
    in-memory timestamp `HeartbeatWatchdog` reads.
    Never raises: a failure to write a liveness signal must never itself be
    treated as a reason to crash the loop it's meant to describe. If `/tmp`
    is ever unwritable, the correct outcome is that the healthcheck
    eventually reports unhealthy -- not a `WorkerRuntime`/`Poller` crash.
    """
    global _last_beat
    _last_beat = time.monotonic()
    with contextlib.suppress(OSError):
        HEARTBEAT_PATH.touch()


def seconds_since_heartbeat() -> float:
    return time.monotonic() - _last_beat


def _hard_exit() -> None:
    os._exit(WATCHDOG_EXIT_CODE)


class HeartbeatWatchdog:
    """Terminates this process if its loop stops heartbeating.

    A daemon thread, because the stuck main thread cannot check itself. It
    reads the in-memory timestamp `touch_heartbeat()` refreshes -- the same
    once-per-iteration signal the `HEALTHCHECK` file carries, but immune to
    filesystem delays or an unwritable `/tmp`. It exits via `os._exit` (no
    cleanup: the main thread is, by assumption, unresponsive), after logging why.

    Once `stop_event` is set (SIGTERM, or the runtime's own self-retirement)
    the loop is *supposed* to stop heartbeating, so staleness is no longer a
    fault; the watchdog then only guards against a shutdown that itself hangs,
    firing if the process is still alive `stale_seconds` after the stop was
    requested.
    """

    def __init__(
        self,
        *,
        stale_seconds: float,
        stop_event: threading.Event,
        service: str,
        check_interval_seconds: float = 5.0,
        age_fn: Callable[[], float] = seconds_since_heartbeat,
        exit_fn: Callable[[], None] = _hard_exit,
        clock: Callable[[], float] = time.monotonic,
        log_grace_seconds: float = 5.0,
    ) -> None:
        self._stale_seconds = stale_seconds
        self._stop_event = stop_event
        self._service = service
        self._check_interval_seconds = check_interval_seconds
        self._age_fn = age_fn
        self._exit_fn = exit_fn
        self._clock = clock
        self._log_grace_seconds = log_grace_seconds
        self._stop_requested_at: float | None = None

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self._run, name=f"{self._service}-heartbeat-watchdog", daemon=True
        )
        thread.start()
        return thread

    def _run(self) -> None:
        while True:
            time.sleep(self._check_interval_seconds)
            try:
                self.check_once()
            except Exception:
                # The guard must never die silently: a bug here would leave a
                # stuck worker with no watchdog and no trace.
                logger.exception("%s heartbeat watchdog check failed", self._service)

    def _force_exit(self, message: str, *args: object) -> None:
        # Arm an independent backstop *before* logging: if the stall is a
        # blocked stdout write, the main thread holds the logging handler's
        # lock, so `logger.critical` below would block this thread forever
        # and the exit would never happen. The timer thread doesn't touch
        # logging. In production `exit_fn` never returns.
        backstop = threading.Timer(self._log_grace_seconds, self._exit_fn)
        backstop.daemon = True
        backstop.start()
        logger.critical(message, *args)
        self._exit_fn()
        backstop.cancel()

    def check_once(self) -> None:
        if self._stop_event.is_set():
            if self._stop_requested_at is None:
                self._stop_requested_at = self._clock()
            if self._clock() - self._stop_requested_at > self._stale_seconds:
                self._force_exit(
                    "%s shutdown did not complete within %.0fs of the stop request "
                    "-- forcing exit so the container restart policy takes over",
                    self._service,
                    self._stale_seconds,
                )
            return

        age = self._age_fn()
        if age > self._stale_seconds:
            self._force_exit(
                "%s loop has not heartbeated for %.0fs (limit %.0fs) -- forcing exit "
                "so the container restart policy takes over",
                self._service,
                age,
                self._stale_seconds,
            )
