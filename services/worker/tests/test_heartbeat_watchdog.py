"""Unit tests for worker.heartbeat.HeartbeatWatchdog -- the in-process guard that
force-exits a worker/poller whose loop stopped heartbeating, so Docker's
`restart: unless-stopped` recovers the container (a `HEALTHCHECK` alone never
restarts anything).

`check_once()` is driven directly with an injected age/clock/exit, so almost
nothing here sleeps; one test runs the real daemon thread end to end.
"""

from __future__ import annotations

import threading
import time

import worker.heartbeat as heartbeat
from worker.heartbeat import HeartbeatWatchdog


class _Recorder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _watchdog(
    *,
    age: float,
    stale_seconds: float = 100.0,
    stop_event: threading.Event | None = None,
    now: list[float] | None = None,
) -> tuple[HeartbeatWatchdog, _Recorder]:
    recorder = _Recorder()
    clock_value = now if now is not None else [0.0]
    watchdog = HeartbeatWatchdog(
        stale_seconds=stale_seconds,
        stop_event=stop_event or threading.Event(),
        service="worker",
        age_fn=lambda: age,
        exit_fn=recorder,
        clock=lambda: clock_value[0],
    )
    return watchdog, recorder


def test_stale_heartbeat_forces_exit() -> None:
    watchdog, exit_fn = _watchdog(age=101.0)

    watchdog.check_once()

    assert exit_fn.calls == 1


def test_fresh_heartbeat_does_not_exit() -> None:
    watchdog, exit_fn = _watchdog(age=5.0)

    watchdog.check_once()

    assert exit_fn.calls == 0


def test_exactly_at_the_limit_does_not_exit() -> None:
    watchdog, exit_fn = _watchdog(age=100.0)

    watchdog.check_once()

    assert exit_fn.calls == 0


def test_stale_heartbeat_is_not_a_fault_once_shutdown_was_requested() -> None:
    """After SIGTERM / self-retirement the loop is meant to stop
    heartbeating; a graceful drain must not be killed for that."""
    stop_event = threading.Event()
    stop_event.set()
    watchdog, exit_fn = _watchdog(age=10_000.0, stop_event=stop_event)

    watchdog.check_once()

    assert exit_fn.calls == 0


def test_shutdown_that_hangs_past_the_limit_forces_exit() -> None:
    stop_event = threading.Event()
    now = [1000.0]
    watchdog, exit_fn = _watchdog(age=10_000.0, stale_seconds=100.0, stop_event=stop_event, now=now)

    stop_event.set()
    watchdog.check_once()  # notices the stop request at t=1000
    now[0] = 1100.0
    watchdog.check_once()  # exactly at the limit: still draining
    assert exit_fn.calls == 0

    now[0] = 1100.5
    watchdog.check_once()  # process is still alive past the limit
    assert exit_fn.calls == 1


def test_a_recovering_heartbeat_resets_the_verdict() -> None:
    age = [500.0]
    recorder = _Recorder()
    watchdog = HeartbeatWatchdog(
        stale_seconds=100.0,
        stop_event=threading.Event(),
        service="poller",
        age_fn=lambda: age[0],
        exit_fn=recorder,
    )

    age[0] = 1.0  # loop resumed before the next check
    watchdog.check_once()

    assert recorder.calls == 0


def test_touch_heartbeat_refreshes_the_in_memory_age_even_if_the_file_is_unwritable(
    tmp_path, monkeypatch
) -> None:
    """The watchdog must not depend on `/tmp`: a failing filesystem is a
    healthcheck problem, never a reason to kill (or fail to keep alive) a
    loop that is actually making progress."""
    monkeypatch.setattr(heartbeat, "HEARTBEAT_PATH", tmp_path / "missing-dir" / "heartbeat")
    monkeypatch.setattr(heartbeat, "_last_beat", time.monotonic() - 1000.0)
    assert heartbeat.seconds_since_heartbeat() > 900.0

    heartbeat.touch_heartbeat()

    assert heartbeat.seconds_since_heartbeat() < 5.0


def test_default_exit_uses_the_restart_triggering_nonzero_status(monkeypatch) -> None:
    exited_with: list[int] = []
    monkeypatch.setattr(heartbeat.os, "_exit", exited_with.append)

    heartbeat._hard_exit()

    assert exited_with == [heartbeat.WATCHDOG_EXIT_CODE]
    assert heartbeat.WATCHDOG_EXIT_CODE != 0


def test_running_thread_fires_on_a_stale_heartbeat() -> None:
    fired = threading.Event()
    stop = threading.Event()

    def fire() -> None:
        fired.set()
        stop.set()  # after firing, park the thread in its (silent) shutdown branch

    watchdog = HeartbeatWatchdog(
        stale_seconds=100.0,
        stop_event=stop,
        service="worker",
        check_interval_seconds=0.01,
        age_fn=lambda: 10_000.0,
        exit_fn=fire,
    )
    thread = watchdog.start()

    assert thread.daemon  # never keeps a healthy process from exiting
    assert fired.wait(timeout=5.0)


def test_thread_survives_a_failing_check_instead_of_dying_silently() -> None:
    fired = threading.Event()
    stop = threading.Event()

    def fire() -> None:
        fired.set()
        stop.set()  # after firing, park the thread in its (silent) shutdown branch

    calls = [0]

    def flaky_age() -> float:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("boom")
        return 10_000.0

    HeartbeatWatchdog(
        stale_seconds=100.0,
        stop_event=stop,
        service="worker",
        check_interval_seconds=0.01,
        age_fn=flaky_age,
        exit_fn=fire,
    ).start()

    assert fired.wait(timeout=5.0)  # the second check still ran and fired


def test_exit_happens_even_if_logging_blocks(monkeypatch) -> None:
    """If the stall is a blocked stdout write, the main thread holds the
    logging handler lock and `logger.critical` would block the watchdog too;
    the independent backstop timer must still terminate the process."""
    release = threading.Event()
    monkeypatch.setattr(heartbeat.logger, "critical", lambda *a, **k: release.wait(10.0))
    fired = threading.Event()
    watchdog = HeartbeatWatchdog(
        stale_seconds=100.0,
        stop_event=threading.Event(),
        service="worker",
        age_fn=lambda: 10_000.0,
        exit_fn=fired.set,
        log_grace_seconds=0.05,
    )

    blocked = threading.Thread(target=watchdog.check_once, daemon=True)
    blocked.start()
    try:
        assert fired.wait(timeout=5.0)
    finally:
        release.set()
        blocked.join(timeout=5.0)
