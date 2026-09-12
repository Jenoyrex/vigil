"""Unit tests for worker.runtime.WorkerRuntime / generate_worker_id.

Uses a self-contained fake connection (not tests/conftest.py's
fake_postgres_connection, which has no `.close()` -- WorkerRuntime always
closes its per-tick connection, and that's exactly one of the things these
tests verify) and a hand-rolled fake Dispatcher (WorkerRuntime never
constructs a real one -- it's injected, per the approved design). Real
claim_jobs/reap_stuck_jobs SQL correctness is already covered by
test_evaluation_jobs_repository.py/test_reaper.py and their real-PostgreSQL
counterparts -- not re-tested here. These tests are about WorkerRuntime's
own scheduling/shutdown/connection-lifecycle logic.

Tick-level behavior (claim/reap in isolation) is tested by calling
`_claim_and_dispatch`/`_reap` directly -- private, but this is the same
"test the unit, not just the public loop" tradeoff `run()`'s own signal
installation and startup reap would otherwise force every test to route
around. Loop/scheduling/shutdown tests use the public `run()`.

`signal.signal` is patched to a no-op for every test in this module except
the one that specifically verifies signal wiring (which installs its own
recording patch) -- so no test in this file ever touches this process's
real SIGTERM/SIGINT handlers.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import call, patch

import pytest

from worker.dispatcher import DispatchOutcome
from worker.postgres.repository import ClaimedJob
from worker.runtime import WorkerRuntime, generate_worker_id

PROJECT_ID = uuid.uuid4()
CREATED_AT = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_real_signal_handlers(monkeypatch):
    """Every test in this module gets a no-op signal.signal by default --
    run()'s own _install_signal_handlers() call must never register a real
    OS-level handler during a test run. test_install_signal_handlers_wires_sigterm_and_sigint
    installs its own recording patch, which simply overrides this one for
    the duration of that single test (monkeypatch scoping)."""
    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)


# -- fakes --------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows: list[tuple], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """One fake `psycopg.Connection`-shaped object per simulated
    `jobs_connection_factory()` call. Supports queued (rows, rowcount)
    responses in call order (mirrors tests/conftest.py's
    FakePostgresConnection), plus what that fake doesn't have:
    `.close()` (tracked), an `on_execute` hook (used to trigger a stop mid-
    tick, or to inject a real sleep to let monotonic time actually advance),
    a shared `call_order` tag (proves ordering across ticks, e.g. "reap
    happens before the first claim"), and `fail_with` (simulates a real
    connection/statement failure).
    """

    def __init__(self, *, tag: str = "", call_order: list[str] | None = None) -> None:
        self.closed = False
        self.queries: list[tuple[str, dict]] = []
        self._responses: list[tuple[list[tuple], int]] = []
        self.fail_with: Exception | None = None
        self.on_execute: Callable[[], None] | None = None
        self._tag = tag
        self._call_order = call_order

    def queue_result(self, rows: list[tuple] = (), rowcount: int = 0) -> None:
        self._responses.append((list(rows), rowcount))

    def execute(self, query: str, params: dict | None = None) -> _FakeCursor:
        if self._call_order is not None:
            self._call_order.append(self._tag)
        if self.on_execute is not None:
            self.on_execute()
        if self.fail_with is not None:
            raise self.fail_with
        self.queries.append((query, params or {}))
        rows, rowcount = self._responses.pop(0) if self._responses else ([], 0)
        return _FakeCursor(rows, rowcount)

    def close(self) -> None:
        self.closed = True


def _connection_factory(connections: list[_FakeConnection]) -> Callable[[], _FakeConnection]:
    """Hands out `connections` in order, one per call -- mirrors
    `get_connection()`'s "brand-new connection every call" contract. Raises
    if a test's scenario needs more connections than it provisioned --
    forces every test to be explicit about exactly how many ticks it
    expects to open a connection for."""
    iterator = iter(connections)
    return lambda: next(iterator)


class FakeDispatcher:
    """Records every `dispatch(jobs)` call (list of ClaimedJob, in order);
    returns a scripted outcomes list if given, else one successful
    DispatchOutcome per job. `on_dispatch`, if set, runs after recording the
    call and before returning -- used to trigger a stop mid-tick."""

    def __init__(
        self,
        *,
        outcomes: list[DispatchOutcome] | None = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> None:
        self.dispatch_calls: list[list[ClaimedJob]] = []
        self._outcomes = outcomes
        self.on_dispatch = on_dispatch

    def dispatch(self, jobs: list[ClaimedJob]) -> list[DispatchOutcome]:
        self.dispatch_calls.append(list(jobs))
        if self.on_dispatch is not None:
            self.on_dispatch()
        if self._outcomes is not None:
            return self._outcomes
        return [DispatchOutcome(job=job, evaluation_outcome=None, error=None) for job in jobs]


def _claimed_job(*, job_id: uuid.UUID | None = None, attempt_count: int = 1) -> ClaimedJob:
    return ClaimedJob(
        id=job_id or uuid.uuid4(),
        project_id=PROJECT_ID,
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        span_id="00f067aa0ba902b7",
        evaluator_name="relevance_embedding",
        evaluator_version="0.1.0",
        attempt_count=attempt_count,
        max_retries=3,
        created_at=CREATED_AT,
    )


def _claim_row(job: ClaimedJob) -> tuple:
    # Matches _CLAIM_RETURNING_COLUMNS' order in worker/postgres/repository.py
    # (also ClaimedJob's own field order) -- hardcoded here rather than
    # importing that private constant, matching
    # test_evaluation_jobs_repository.py's existing convention.
    return (
        job.id,
        job.project_id,
        job.trace_id,
        job.span_id,
        job.evaluator_name,
        job.evaluator_version,
        job.attempt_count,
        job.max_retries,
        job.created_at,
    )


def _make_runtime(
    *,
    dispatcher,
    connections: list[_FakeConnection],
    worker_id: str = "test-worker",
    claim_batch_size: int = 4,
    poll_interval_seconds: float = 5.0,
    reaper_interval_seconds: float = 1000.0,
    stuck_job_threshold_seconds: float = 900.0,
    reaper_batch_size: int = 100,
    max_orphaned_evaluator_threads: int = 4,
) -> WorkerRuntime:
    return WorkerRuntime(
        dispatcher=dispatcher,
        jobs_connection_factory=_connection_factory(connections),
        worker_id=worker_id,
        claim_batch_size=claim_batch_size,
        poll_interval_seconds=poll_interval_seconds,
        reaper_interval_seconds=reaper_interval_seconds,
        stuck_job_threshold_seconds=stuck_job_threshold_seconds,
        reaper_batch_size=reaper_batch_size,
        max_orphaned_evaluator_threads=max_orphaned_evaluator_threads,
    )


# -- 1/2: claim tick behavior --------------------------------------------------


def test_claim_returns_jobs_dispatch_receives_exactly_those_jobs() -> None:
    job = _claimed_job()
    connection = _FakeConnection()
    connection.queue_result(rows=[_claim_row(job)])
    dispatcher = FakeDispatcher()
    runtime = _make_runtime(dispatcher=dispatcher, connections=[connection])

    did_work = runtime._claim_and_dispatch()

    assert did_work is True
    assert dispatcher.dispatch_calls == [[job]]
    assert connection.closed is True


def test_empty_claim_dispatch_not_called() -> None:
    connection = _FakeConnection()
    connection.queue_result(rows=[])
    dispatcher = FakeDispatcher()
    runtime = _make_runtime(dispatcher=dispatcher, connections=[connection])

    did_work = runtime._claim_and_dispatch()

    assert did_work is False
    assert dispatcher.dispatch_calls == []
    assert connection.closed is True


def test_claim_connection_is_closed_before_dispatch_begins() -> None:
    """The connection must be closed before dispatch() is even called, not
    merely closed eventually -- Dispatcher resolves its own, separate
    per-job connections and must never see the claim connection still open."""
    job = _claimed_job()
    connection = _FakeConnection()
    connection.queue_result(rows=[_claim_row(job)])
    closed_before_dispatch = []

    def _on_dispatch() -> None:
        closed_before_dispatch.append(connection.closed)

    dispatcher = FakeDispatcher(on_dispatch=_on_dispatch)
    runtime = _make_runtime(dispatcher=dispatcher, connections=[connection])

    runtime._claim_and_dispatch()

    assert closed_before_dispatch == [True]


# -- 7/8: connection failures don't kill the runtime ---------------------------


def test_claim_connection_failure_does_not_kill_runtime() -> None:
    connection = _FakeConnection()
    connection.fail_with = RuntimeError("PostgreSQL unreachable")
    dispatcher = FakeDispatcher()
    runtime = _make_runtime(dispatcher=dispatcher, connections=[connection])

    did_work = runtime._claim_and_dispatch()  # must not raise

    assert did_work is False
    assert dispatcher.dispatch_calls == []


def test_reaper_connection_failure_does_not_kill_runtime() -> None:
    connection = _FakeConnection()
    connection.fail_with = RuntimeError("PostgreSQL unreachable")
    runtime = _make_runtime(dispatcher=FakeDispatcher(), connections=[connection])

    runtime._reap()  # must not raise


# -- 9/10: worker_id -----------------------------------------------------------


def test_generate_worker_id_shape() -> None:
    worker_id = generate_worker_id()
    hostname = socket.gethostname()
    pid = os.getpid()
    prefix = f"{hostname}:{pid}:"
    assert worker_id.startswith(prefix)
    suffix = worker_id[len(prefix) :]
    assert len(suffix) == 8


def test_generate_worker_id_is_unique_per_call() -> None:
    assert generate_worker_id() != generate_worker_id()


def test_same_worker_id_used_across_multiple_claim_ticks() -> None:
    connection_a = _FakeConnection()
    connection_a.queue_result(rows=[])
    connection_b = _FakeConnection()
    connection_b.queue_result(rows=[])
    runtime = _make_runtime(
        dispatcher=FakeDispatcher(),
        connections=[connection_a, connection_b],
        worker_id="fixed-worker-id",
    )

    runtime._claim_and_dispatch()
    runtime._claim_and_dispatch()

    assert connection_a.queries[0][1]["worker_id"] == "fixed-worker-id"
    assert connection_b.queries[0][1]["worker_id"] == "fixed-worker-id"


# -- 3: startup reap occurs before first claim ---------------------------------


def test_startup_reap_occurs_before_first_claim() -> None:
    call_order: list[str] = []
    reap_conn = _FakeConnection(tag="reap", call_order=call_order)
    reap_conn.queue_result(rows=[])
    claim_conn = _FakeConnection(tag="claim", call_order=call_order)
    claim_conn.queue_result(rows=[])

    runtime = _make_runtime(
        dispatcher=FakeDispatcher(),
        connections=[reap_conn, claim_conn],
        reaper_interval_seconds=1000.0,
        poll_interval_seconds=0.01,
    )
    claim_conn.on_execute = runtime.request_stop  # stop right after the first claim

    runtime.run()

    assert call_order == ["reap", "claim"]


# -- 4: reaper respects its interval -------------------------------------------


def test_reaper_does_not_run_again_before_interval_elapses() -> None:
    call_order: list[str] = []
    reap_conn = _FakeConnection(tag="reap", call_order=call_order)
    reap_conn.queue_result(rows=[])
    claim_conn = _FakeConnection(tag="claim", call_order=call_order)
    claim_conn.queue_result(rows=[])

    runtime = _make_runtime(
        dispatcher=FakeDispatcher(),
        connections=[reap_conn, claim_conn],
        reaper_interval_seconds=1000.0,  # nowhere near due
        poll_interval_seconds=0.01,
    )
    claim_conn.on_execute = runtime.request_stop

    runtime.run()

    assert call_order == ["reap", "claim"]  # no second "reap"


def test_reaper_runs_again_once_interval_elapses() -> None:
    call_order: list[str] = []
    reap_conn = _FakeConnection(tag="reap", call_order=call_order)
    reap_conn.queue_result(rows=[])
    claim_conn = _FakeConnection(tag="claim", call_order=call_order)
    claim_conn.queue_result(rows=[])
    claim_conn.on_execute = lambda: time.sleep(0.05)  # let monotonic time advance
    second_reap_conn = _FakeConnection(tag="reap", call_order=call_order)
    second_reap_conn.queue_result(rows=[])
    second_claim_conn = _FakeConnection(tag="claim", call_order=call_order)
    second_claim_conn.queue_result(rows=[])

    runtime = _make_runtime(
        dispatcher=FakeDispatcher(),
        connections=[reap_conn, claim_conn, second_reap_conn, second_claim_conn],
        reaper_interval_seconds=0.02,  # comfortably less than the 0.05s sleep above
        poll_interval_seconds=0.01,
    )
    second_claim_conn.on_execute = runtime.request_stop

    runtime.run()

    assert call_order == ["reap", "claim", "reap", "claim"]


# -- 5: stop between claim and reap prevents unnecessary reap -----------------


def test_stop_between_claim_and_reap_prevents_unnecessary_reap() -> None:
    call_order: list[str] = []
    reap_conn = _FakeConnection(tag="reap", call_order=call_order)
    reap_conn.queue_result(rows=[])
    claim_conn = _FakeConnection(tag="claim", call_order=call_order)
    claim_conn.queue_result(rows=[])

    runtime = _make_runtime(
        dispatcher=FakeDispatcher(),
        connections=[reap_conn, claim_conn],
        reaper_interval_seconds=0.0,  # "due" immediately if it were ever checked
        poll_interval_seconds=0.01,
    )
    claim_conn.on_execute = runtime.request_stop

    runtime.run()

    # Only the startup reap and the one claim -- the stop set during that
    # claim must be observed before the (otherwise-due) reap check runs.
    assert call_order == ["reap", "claim"]


# -- 6: stop during idle wait exits promptly -----------------------------------


def test_stop_during_idle_wait_exits_promptly() -> None:
    reap_conn = _FakeConnection()
    reap_conn.queue_result(rows=[])
    claim_conn = _FakeConnection()
    claim_conn.queue_result(rows=[])  # empty -> idle -> would wait poll_interval_seconds

    runtime = _make_runtime(
        dispatcher=FakeDispatcher(),
        connections=[reap_conn, claim_conn],
        reaper_interval_seconds=1000.0,
        poll_interval_seconds=30.0,  # long enough to prove interruption, not luck
    )
    threading.Timer(0.1, runtime.request_stop).start()

    start = time.monotonic()
    runtime.run()
    elapsed = time.monotonic() - start

    assert elapsed < 5.0  # nowhere near the full 30s poll interval


# -- 11: busy loop does not sleep after a successful claim ---------------------


def test_busy_loop_does_not_sleep_between_successive_successful_claims() -> None:
    reap_conn = _FakeConnection()
    reap_conn.queue_result(rows=[])
    job_1 = _claimed_job()
    claim_conn_1 = _FakeConnection()
    claim_conn_1.queue_result(rows=[_claim_row(job_1)])
    job_2 = _claimed_job()
    claim_conn_2 = _FakeConnection()
    claim_conn_2.queue_result(rows=[_claim_row(job_2)])

    dispatcher = FakeDispatcher()
    runtime = _make_runtime(
        dispatcher=dispatcher,
        connections=[reap_conn, claim_conn_1, claim_conn_2],
        reaper_interval_seconds=1000.0,
        poll_interval_seconds=30.0,  # would dominate elapsed time if ever waited on
    )
    # Stop only once the SECOND claim's dispatch runs, proving the loop moved
    # straight from claim_conn_1's successful claim to claim_conn_2 with no
    # idle wait in between.
    calls = []

    def _on_dispatch() -> None:
        calls.append(1)
        if len(calls) == 2:
            runtime.request_stop()

    dispatcher.on_dispatch = _on_dispatch

    start = time.monotonic()
    runtime.run()
    elapsed = time.monotonic() - start

    assert dispatcher.dispatch_calls == [[job_1], [job_2]]
    assert elapsed < 5.0  # no 30s wait was ever taken between the two claims


# -- 12: idle loop waits using interruptible stop_event.wait() ----------------


def test_idle_loop_waits_via_stop_event_wait_with_configured_interval() -> None:
    reap_conn = _FakeConnection()
    reap_conn.queue_result(rows=[])
    claim_conn = _FakeConnection()
    claim_conn.queue_result(rows=[])

    runtime = _make_runtime(
        dispatcher=FakeDispatcher(),
        connections=[reap_conn, claim_conn],
        reaper_interval_seconds=1000.0,
        poll_interval_seconds=12.34,
    )

    wait_calls: list[float | None] = []

    def _spy_wait(timeout: float | None = None) -> bool:
        wait_calls.append(timeout)
        runtime._stop_event.set()
        return True

    runtime._stop_event.wait = _spy_wait

    runtime.run()

    assert wait_calls == [12.34]


# -- 13: signal handling requests stop -----------------------------------------


def test_handle_signal_requests_stop() -> None:
    runtime = _make_runtime(dispatcher=FakeDispatcher(), connections=[])

    runtime._handle_signal(signal.SIGTERM, None)

    assert runtime._stop_event.is_set() is True


def test_install_signal_handlers_wires_sigterm_and_sigint() -> None:
    runtime = _make_runtime(dispatcher=FakeDispatcher(), connections=[])

    with patch("worker.runtime.signal.signal") as mock_signal:
        runtime._install_signal_handlers()

    mock_signal.assert_has_calls(
        [call(signal.SIGTERM, runtime._handle_signal), call(signal.SIGINT, runtime._handle_signal)],
        any_order=True,
    )


# -- 14: runtime stop exits cleanly --------------------------------------------


def test_stop_requested_before_run_still_performs_startup_reap_then_exits() -> None:
    reap_conn = _FakeConnection()
    reap_conn.queue_result(rows=[])
    dispatcher = FakeDispatcher()
    runtime = _make_runtime(dispatcher=dispatcher, connections=[reap_conn])
    runtime.request_stop()

    runtime.run()  # must return promptly, not hang or raise

    assert reap_conn.queries  # startup reap still ran
    assert dispatcher.dispatch_calls == []  # loop body never entered


# -- 15: bounded-orphan self-restart (Phase 4A) -------------------------------


def test_reaching_the_orphan_threshold_requests_stop_and_exits_cleanly() -> None:
    """Once `outstanding_orphaned_calls()` reaches `max_orphaned_evaluator_
    threads`, the runtime must call its own `request_stop()` -- the exact
    same graceful-shutdown path a SIGTERM already triggers -- and `run()`
    must return promptly, never hang or raise."""
    startup_reap_conn = _FakeConnection()
    startup_reap_conn.queue_result(rows=[])
    claim_conn = _FakeConnection()
    claim_conn.queue_result(rows=[])  # empty claim tick

    dispatcher = FakeDispatcher()
    runtime = _make_runtime(
        dispatcher=dispatcher,
        connections=[startup_reap_conn, claim_conn],
        max_orphaned_evaluator_threads=4,
    )

    with patch("worker.runtime.outstanding_orphaned_calls", return_value=4):
        runtime.run()  # must return promptly, not hang or raise

    assert runtime._stop_event.is_set() is True


def test_below_the_orphan_threshold_does_not_request_stop() -> None:
    """Calls `_check_orphaned_evaluator_threshold()` directly -- not via
    `run()` -- and asserts `_stop_event` remains unset. A prior version of
    this test routed through `run()` with a wrapper that manually called
    `request_stop()` itself whenever the real check hadn't already done so;
    that made the test pass identically regardless of whether the real
    threshold comparison was correct, since the loop would exit after one
    tick either way. Calling the real method directly, with no wrapper,
    means this test only passes if the real comparison genuinely leaves
    `_stop_event` unset below the configured threshold."""
    runtime = _make_runtime(
        dispatcher=FakeDispatcher(), connections=[], max_orphaned_evaluator_threads=4
    )

    with patch("worker.runtime.outstanding_orphaned_calls", return_value=3):
        runtime._check_orphaned_evaluator_threshold()

    assert runtime._stop_event.is_set() is False


def test_orphan_threshold_check_logs_worker_id_count_and_limit(caplog) -> None:
    """`worker_id` deliberately contains no digits at all -- so the outstanding
    count (5) and limit (4) substring checks below can only be satisfied by
    those actual values appearing in the log message, never incidentally by
    the worker_id itself (a prior version of this test used a worker_id
    ending in "...1234", whose own trailing "4" alone would have satisfied
    the "4" in record.message check regardless of whether the real limit
    value was ever correctly formatted into the message). Also asserts the
    exact formatted substrings runtime.py's own log line produces, not just
    bare digit membership."""
    startup_reap_conn = _FakeConnection()
    startup_reap_conn.queue_result(rows=[])
    claim_conn = _FakeConnection()
    claim_conn.queue_result(rows=[])

    dispatcher = FakeDispatcher()
    runtime = _make_runtime(
        dispatcher=dispatcher,
        connections=[startup_reap_conn, claim_conn],
        worker_id="worker-alpha:pid-beta:token-gamma",
        max_orphaned_evaluator_threads=4,
    )

    with (
        patch("worker.runtime.outstanding_orphaned_calls", return_value=5),
        caplog.at_level("ERROR"),
    ):
        runtime.run()

    assert any(
        "worker-alpha:pid-beta:token-gamma" in record.message
        and "5 hung evaluator call(s) outstanding" in record.message
        and "(limit 4)" in record.message
        for record in caplog.records
    )
