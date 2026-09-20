"""Unit tests for worker.poller.Poller / HttpJobCreationClient / _compute_scan_window.

Real ClickHouse/PostgreSQL/HTTP behavior is proven separately in
test_eligible_span_repository_clickhouse_integration.py,
test_poller_checkpoint_repository_postgres_integration.py, and the flagship
end-to-end test (test_poller_flagship_e2e.py). These tests are about
Poller.run_one_tick's own orchestration: definitive-vs-unresolved outcome
handling, all-or-nothing checkpoint advancement, per-evaluator fan-out, and
per-tick failure isolation.
"""

from __future__ import annotations

import contextlib
import json
import signal
import urllib.error
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import call, patch

from worker.clickhouse.eligible_span_repository import EligibleSpan
from worker.poller import HttpJobCreationClient, JobCreationOutcome, Poller, _compute_scan_window
from worker.registry import EvaluatorRegistry

PROJECT_ID = uuid.uuid4()


# -- fakes ----------------------------------------------------------------


class FakeEligibleSpanRepository:
    def __init__(
        self, spans: list[EligibleSpan] | None = None, fail_with: Exception | None = None
    ) -> None:
        self.spans = spans or []
        self.fail_with = fail_with
        self.calls: list[dict[str, Any]] = []

    def select_eligible_spans(
        self, *, lower_bound, start_date_hint, batch_size
    ) -> list[EligibleSpan]:
        self.calls.append(
            {
                "lower_bound": lower_bound,
                "start_date_hint": start_date_hint,
                "batch_size": batch_size,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with
        return self.spans


class _FakeCursor:
    def __init__(self, rows, rowcount) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """Minimal fake psycopg.Connection: queued (rows, rowcount) responses in
    call order, plus `.close()` (tracked) -- mirrors test_runtime.py's
    identical fake, needed here for the same reason: PollerCheckpointRepository
    calls `.close()` on every connection it's given."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.closed = False
        self.queries: list[tuple[str, dict]] = []
        self._responses: list[tuple[list[tuple], int]] = []
        self.fail_with = fail_with

    def queue_result(self, rows=(), rowcount=0) -> None:
        self._responses.append((list(rows), rowcount))

    def execute(self, query: str, params: dict | None = None) -> _FakeCursor:
        if self.fail_with is not None:
            raise self.fail_with
        self.queries.append((query, params or {}))
        rows, rowcount = self._responses.pop(0) if self._responses else ([], 0)
        return _FakeCursor(rows, rowcount)

    def close(self) -> None:
        self.closed = True


def _connection_factory(connections: list[_FakeConnection]):
    iterator = iter(connections)
    return lambda: next(iterator)


class FakeJobCreationClient:
    """Records every call; returns a scripted outcome (default: resolved
    "created") or raises, per-call, keyed by (span_id, evaluator_name)."""

    def __init__(self, *, default_reason: str = "created") -> None:
        self.calls: list[tuple] = []
        self._default_reason = default_reason
        self.outcomes: dict[tuple[str, str], JobCreationOutcome] = {}
        self.raise_for: set[tuple[str, str]] = set()

    def __call__(self, project_id, trace_id, span_id, evaluator_name, evaluator_version):
        self.calls.append((project_id, trace_id, span_id, evaluator_name, evaluator_version))
        key = (span_id, evaluator_name)
        if key in self.raise_for:
            raise RuntimeError("synthetic job-creation failure")
        if key in self.outcomes:
            return self.outcomes[key]
        return JobCreationOutcome(resolved=True, reason=self._default_reason, job_id=uuid.uuid4())


def _span(*, span_id: str = "sp1", ingested_at: datetime) -> EligibleSpan:
    return EligibleSpan(
        project_id=PROJECT_ID,
        trace_id=uuid.uuid4().hex,
        span_id=span_id,
        ingested_at=ingested_at,
    )


def _registry(*pairs: tuple[str, str]) -> EvaluatorRegistry:
    class _FakeEvaluator:
        def __init__(self, name: str, version: str) -> None:
            self.name = name
            self.version = version

        def evaluate(self, *a, **k):
            raise AssertionError("never called by the poller")

    return EvaluatorRegistry([_FakeEvaluator(n, v) for n, v in pairs])


def _make_poller(
    *,
    eligible_span_repository,
    checkpoint_connections: list[_FakeConnection],
    job_creation_client,
    registry: EvaluatorRegistry,
    poller_overlap_seconds: float = 60.0,
    poller_start_time_lookback_days: int = 3,
    poller_interval_seconds: float = 30.0,
    heartbeat_callback=None,
    watchdog_stale_seconds=None,
) -> Poller:
    kwargs = {}
    if heartbeat_callback is not None:
        kwargs["heartbeat_callback"] = heartbeat_callback
    return Poller(
        eligible_span_repository=eligible_span_repository,
        checkpoint_connection_factory=_connection_factory(checkpoint_connections),
        job_creation_client=job_creation_client,
        registry=registry,
        poller_batch_size=500,
        poller_overlap_seconds=poller_overlap_seconds,
        poller_start_time_lookback_days=poller_start_time_lookback_days,
        poller_interval_seconds=poller_interval_seconds,
        watchdog_stale_seconds=watchdog_stale_seconds,
        **kwargs,
    )


# -- full success advances the checkpoint ----------------------------------


def test_full_successful_batch_advances_checkpoint() -> None:
    ingested_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    span_repo = FakeEligibleSpanRepository(spans=[_span(ingested_at=ingested_at)])
    read_conn = _FakeConnection()
    read_conn.queue_result(rowcount=0)  # ensure-row
    read_conn.queue_result(rows=[(None,)])  # get watermark -> None
    advance_conn = _FakeConnection()
    advance_conn.queue_result(rowcount=1)  # CAS succeeds
    job_client = FakeJobCreationClient()
    registry = _registry(("relevance", "0.1.0"))

    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn, advance_conn],
        job_creation_client=job_client,
        registry=registry,
    )
    poller.run_one_tick()

    assert len(job_client.calls) == 1
    advance_query, advance_params = advance_conn.queries[0]
    assert advance_params["new_watermark"] == ingested_at
    assert advance_params["observed_watermark"] is None
    assert read_conn.closed is True
    assert advance_conn.closed is True


def test_calls_job_creation_once_per_span_per_installed_evaluator() -> None:
    ingested_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    spans = [
        _span(span_id="sp1", ingested_at=ingested_at),
        _span(span_id="sp2", ingested_at=ingested_at),
    ]
    span_repo = FakeEligibleSpanRepository(spans=spans)
    read_conn = _FakeConnection()
    read_conn.queue_result(rowcount=0)
    read_conn.queue_result(rows=[(None,)])
    advance_conn = _FakeConnection()
    advance_conn.queue_result(rowcount=1)
    job_client = FakeJobCreationClient()
    registry = _registry(("relevance", "0.1.0"), ("relevance_embedding", "0.1.0"))

    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn, advance_conn],
        job_creation_client=job_client,
        registry=registry,
    )
    poller.run_one_tick()

    # 2 spans x 2 evaluators = 4 calls.
    assert len(job_client.calls) == 4
    called_span_evaluator_pairs = {(c[2], c[3]) for c in job_client.calls}
    assert called_span_evaluator_pairs == {
        ("sp1", "relevance"),
        ("sp1", "relevance_embedding"),
        ("sp2", "relevance"),
        ("sp2", "relevance_embedding"),
    }


# -- partial failure does not advance ---------------------------------------


def test_partial_failure_does_not_advance_checkpoint() -> None:
    ingested_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    spans = [
        _span(span_id="sp1", ingested_at=ingested_at),
        _span(span_id="sp2", ingested_at=ingested_at),
    ]
    span_repo = FakeEligibleSpanRepository(spans=spans)
    read_conn = _FakeConnection()
    read_conn.queue_result(rowcount=0)
    read_conn.queue_result(rows=[(None,)])
    job_client = FakeJobCreationClient()
    job_client.outcomes[("sp2", "relevance")] = JobCreationOutcome(resolved=False)
    registry = _registry(("relevance", "0.1.0"))

    # Only ONE connection provisioned (the checkpoint read) -- if the poller
    # tried to advance despite the failure, this test would fail with a
    # StopIteration from the exhausted connection factory, not silently pass.
    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn],
        job_creation_client=job_client,
        registry=registry,
    )
    poller.run_one_tick()

    assert len(job_client.calls) == 2  # both were attempted
    assert read_conn.closed is True


def test_api_outage_does_not_advance_checkpoint() -> None:
    """Every job-creation call raises (simulating total API unavailability)
    -- no candidate resolves, checkpoint must not advance."""
    ingested_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    span_repo = FakeEligibleSpanRepository(spans=[_span(ingested_at=ingested_at)])
    read_conn = _FakeConnection()
    read_conn.queue_result(rowcount=0)
    read_conn.queue_result(rows=[(None,)])
    job_client = FakeJobCreationClient()
    job_client.raise_for.add(("sp1", "relevance"))
    registry = _registry(("relevance", "0.1.0"))

    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn],  # no advance connection provisioned
        job_creation_client=job_client,
        registry=registry,
    )
    poller.run_one_tick()  # must not raise, must not request a second connection


def test_unresolved_reason_from_api_does_not_advance_checkpoint() -> None:
    """A response the client couldn't map to one of the four definitive
    reasons (e.g. a malformed/unexpected payload) must also block
    advancement -- resolved=False, not resolved=True with a garbage
    reason."""
    ingested_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    span_repo = FakeEligibleSpanRepository(spans=[_span(ingested_at=ingested_at)])
    read_conn = _FakeConnection()
    read_conn.queue_result(rowcount=0)
    read_conn.queue_result(rows=[(None,)])
    job_client = FakeJobCreationClient()
    job_client.outcomes[("sp1", "relevance")] = JobCreationOutcome(resolved=False)
    registry = _registry(("relevance", "0.1.0"))

    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn],
        job_creation_client=job_client,
        registry=registry,
    )
    poller.run_one_tick()


# -- crash/replay produces no duplicate progress -----------------------------


def test_crash_before_checkpoint_advance_is_safe_to_retry() -> None:
    """Simulates "the process died before advancing the checkpoint": the
    same tick, re-run from the same (unchanged) observed watermark, must
    behave identically and safely -- job-creation is called again for the
    same spans (idempotent on the real API side, not re-proven here), and
    the checkpoint advances normally on the successful retry."""
    ingested_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    span_repo = FakeEligibleSpanRepository(spans=[_span(ingested_at=ingested_at)])
    job_client = FakeJobCreationClient()
    registry = _registry(("relevance", "0.1.0"))

    # First "attempt": checkpoint read succeeds, but the process "crashes"
    # before advancing -- simulated by simply not calling _advance_checkpoint
    # (no second connection provisioned) and constructing a fresh Poller for
    # the "restart".
    read_conn_1 = _FakeConnection()
    read_conn_1.queue_result(rowcount=0)
    read_conn_1.queue_result(rows=[(None,)])
    crashed_job_client_calls_before = len(job_client.calls)
    poller_1 = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn_1],
        job_creation_client=job_client,
        registry=registry,
    )
    # Force "crash before advance" by monkeypatching _advance_checkpoint to
    # simulate the process dying right at that point.
    poller_1._advance_checkpoint = lambda **_: (_ for _ in ()).throw(SystemExit("simulated crash"))
    with contextlib.suppress(SystemExit):
        poller_1.run_one_tick()
    assert len(job_client.calls) == crashed_job_client_calls_before + 1

    # "Restart": a fresh Poller, checkpoint still unchanged (None), retries
    # the identical tick.
    read_conn_2 = _FakeConnection()
    read_conn_2.queue_result(rowcount=0)
    read_conn_2.queue_result(rows=[(None,)])
    advance_conn = _FakeConnection()
    advance_conn.queue_result(rowcount=1)
    poller_2 = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn_2, advance_conn],
        job_creation_client=job_client,
        registry=registry,
    )
    poller_2.run_one_tick()

    # job-creation was called again (idempotent on the real API) and the
    # checkpoint advanced normally this time.
    assert len(job_client.calls) == crashed_job_client_calls_before + 2
    assert advance_conn.queries[0][1]["new_watermark"] == ingested_at


# -- empty batch behavior -----------------------------------------------------


def test_empty_batch_does_not_manufacture_progress() -> None:
    span_repo = FakeEligibleSpanRepository(spans=[])
    read_conn = _FakeConnection()
    read_conn.queue_result(rowcount=0)
    read_conn.queue_result(rows=[(None,)])
    job_client = FakeJobCreationClient()
    registry = _registry(("relevance", "0.1.0"))

    # No second connection provisioned -- an attempt to advance the
    # checkpoint on an empty batch would raise StopIteration.
    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn],
        job_creation_client=job_client,
        registry=registry,
    )
    poller.run_one_tick()

    assert job_client.calls == []


# -- ClickHouse/checkpoint-read failures don't kill the poller ---------------


def test_clickhouse_scan_failure_does_not_raise() -> None:
    span_repo = FakeEligibleSpanRepository(fail_with=RuntimeError("ClickHouse unavailable"))
    read_conn = _FakeConnection()
    read_conn.queue_result(rowcount=0)
    read_conn.queue_result(rows=[(None,)])
    job_client = FakeJobCreationClient()
    registry = _registry(("relevance", "0.1.0"))

    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn],
        job_creation_client=job_client,
        registry=registry,
    )
    poller.run_one_tick()  # must not raise

    assert job_client.calls == []


def test_checkpoint_read_failure_aborts_before_scanning_clickhouse() -> None:
    span_repo = FakeEligibleSpanRepository(spans=[_span(ingested_at=datetime.now(UTC))])
    read_conn = _FakeConnection(fail_with=RuntimeError("PostgreSQL unreachable"))

    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn],
        job_creation_client=FakeJobCreationClient(),
        registry=_registry(("relevance", "0.1.0")),
    )
    poller.run_one_tick()  # must not raise

    assert span_repo.calls == []  # never even reached the ClickHouse scan


def test_stale_checkpoint_cas_loss_during_advance_is_not_an_error() -> None:
    ingested_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    span_repo = FakeEligibleSpanRepository(spans=[_span(ingested_at=ingested_at)])
    read_conn = _FakeConnection()
    read_conn.queue_result(rowcount=0)
    read_conn.queue_result(rows=[(None,)])
    advance_conn = _FakeConnection()
    advance_conn.queue_result(rowcount=0)  # another poller already advanced it
    job_client = FakeJobCreationClient()

    poller = _make_poller(
        eligible_span_repository=span_repo,
        checkpoint_connections=[read_conn, advance_conn],
        job_creation_client=job_client,
        registry=_registry(("relevance", "0.1.0")),
    )
    poller.run_one_tick()  # must not raise despite the lost CAS race


# -- _compute_scan_window (overlap window) -----------------------------------


def test_scan_window_none_on_very_first_poll() -> None:
    lower_bound, start_date_hint = _compute_scan_window(None, overlap_seconds=60.0, lookback_days=3)
    assert lower_bound is None
    assert start_date_hint is None


def test_scan_window_applies_overlap_to_lower_bound() -> None:
    watermark = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    lower_bound, _ = _compute_scan_window(watermark, overlap_seconds=60.0, lookback_days=3)
    assert lower_bound == watermark - timedelta(seconds=60.0)


def test_scan_window_derives_start_date_hint_from_checkpoint_not_wall_clock() -> None:
    watermark = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    _, start_date_hint = _compute_scan_window(watermark, overlap_seconds=60.0, lookback_days=3)
    assert start_date_hint == date(2026, 9, 7)


# -- HttpJobCreationClient ----------------------------------------------------


def _fake_response(payload: dict) -> Any:
    class _Resp:
        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def test_http_client_sends_expected_request() -> None:
    client = HttpJobCreationClient(
        base_url="http://localhost:8000", internal_service_token="secret", timeout_seconds=5.0
    )
    with patch("worker.poller.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_response(
            {"job_id": str(uuid.uuid4()), "created": True, "reason": "created"}
        )
        client(PROJECT_ID, "trace1", "span1", "relevance", "0.1.0")

    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "http://localhost:8000/v1/evaluations/jobs"
    assert request.get_header("X-vigil-internal-token") == "secret"
    body = json.loads(request.data)
    assert body["project_id"] == str(PROJECT_ID)
    assert body["evaluator_name"] == "relevance"


def test_http_client_maps_created_response() -> None:
    job_id = uuid.uuid4()
    client = HttpJobCreationClient(
        base_url="http://x", internal_service_token="t", timeout_seconds=5.0
    )
    with patch("worker.poller.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_response(
            {"job_id": str(job_id), "created": True, "reason": "created"}
        )
        outcome = client(PROJECT_ID, "t", "s", "relevance", "0.1.0")

    assert outcome == JobCreationOutcome(resolved=True, reason="created", job_id=job_id)


def test_http_client_maps_not_sampled_response_with_null_job_id() -> None:
    client = HttpJobCreationClient(
        base_url="http://x", internal_service_token="t", timeout_seconds=5.0
    )
    with patch("worker.poller.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_response(
            {"job_id": None, "created": False, "reason": "not_sampled"}
        )
        outcome = client(PROJECT_ID, "t", "s", "relevance", "0.1.0")

    assert outcome == JobCreationOutcome(resolved=True, reason="not_sampled", job_id=None)


def test_http_client_404_is_unresolved() -> None:
    client = HttpJobCreationClient(
        base_url="http://x", internal_service_token="t", timeout_seconds=5.0
    )
    with patch("worker.poller.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
        outcome = client(PROJECT_ID, "t", "s", "relevance", "0.1.0")

    assert outcome == JobCreationOutcome(resolved=False)


def test_http_client_5xx_is_unresolved() -> None:
    client = HttpJobCreationClient(
        base_url="http://x", internal_service_token="t", timeout_seconds=5.0
    )
    with patch("worker.poller.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError("http://x", 503, "Unavailable", {}, None)
        outcome = client(PROJECT_ID, "t", "s", "relevance", "0.1.0")

    assert outcome == JobCreationOutcome(resolved=False)


def test_http_client_connection_failure_is_unresolved() -> None:
    client = HttpJobCreationClient(
        base_url="http://x", internal_service_token="t", timeout_seconds=5.0
    )
    with patch("worker.poller.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        outcome = client(PROJECT_ID, "t", "s", "relevance", "0.1.0")

    assert outcome == JobCreationOutcome(resolved=False)


def test_http_client_unrecognized_reason_is_unresolved() -> None:
    client = HttpJobCreationClient(
        base_url="http://x", internal_service_token="t", timeout_seconds=5.0
    )
    with patch("worker.poller.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_response(
            {"job_id": None, "created": False, "reason": "something_unexpected"}
        )
        outcome = client(PROJECT_ID, "t", "s", "relevance", "0.1.0")

    assert outcome == JobCreationOutcome(resolved=False)


# -- signal handling ------------------------------------------------------


def test_handle_signal_requests_stop() -> None:
    poller = _make_poller(
        eligible_span_repository=FakeEligibleSpanRepository(),
        checkpoint_connections=[],
        job_creation_client=FakeJobCreationClient(),
        registry=_registry(),
    )
    poller._handle_signal(signal.SIGTERM, None)
    assert poller._stop_event.is_set() is True


def test_install_signal_handlers_wires_sigterm_and_sigint() -> None:
    poller = _make_poller(
        eligible_span_repository=FakeEligibleSpanRepository(),
        checkpoint_connections=[],
        job_creation_client=FakeJobCreationClient(),
        registry=_registry(),
    )
    with patch("worker.poller.signal.signal") as mock_signal:
        poller._install_signal_handlers()

    mock_signal.assert_has_calls(
        [call(signal.SIGTERM, poller._handle_signal), call(signal.SIGINT, poller._handle_signal)],
        any_order=True,
    )


# -- F6: liveness heartbeat -----------------------------------------------


def _checkpoint_connection_with_no_watermark() -> _FakeConnection:
    """One connection shaped for a single `get_or_create_checkpoint()` call
    that finds no existing row -- matches `PollerCheckpointRepository`'s own
    ensure-row-then-read-watermark query pair (see the full-success test
    above), sufficient for a tick that finds no eligible spans and returns
    before ever touching job creation or the checkpoint-advance path."""
    connection = _FakeConnection()
    connection.queue_result(rowcount=0)  # ensure-row
    connection.queue_result(rows=[(None,)])  # get watermark -> None
    return connection


def test_heartbeat_callback_is_invoked_at_startup_before_first_tick() -> None:
    heartbeat_calls: list[str] = []
    checkpoint_conn = _checkpoint_connection_with_no_watermark()

    def _stop_after_one_tick() -> None:
        heartbeat_calls.append("heartbeat")
        if len(heartbeat_calls) == 2:  # startup + first loop iteration
            poller.request_stop()

    poller = _make_poller(
        eligible_span_repository=FakeEligibleSpanRepository(spans=[]),
        checkpoint_connections=[checkpoint_conn],
        job_creation_client=FakeJobCreationClient(),
        registry=_registry(),
        heartbeat_callback=_stop_after_one_tick,
    )

    with patch("worker.poller.signal.signal"):
        poller.run()

    assert heartbeat_calls == ["heartbeat", "heartbeat"]
    assert checkpoint_conn.closed is True


def test_heartbeat_callback_is_invoked_once_per_loop_iteration() -> None:
    """Three loop iterations (each finding no eligible spans, so each just
    idles) must each produce their own heartbeat call -- proving it fires
    at the top of every iteration, not merely once for the whole run()."""
    heartbeat_calls = 0
    connections = [_checkpoint_connection_with_no_watermark() for _ in range(3)]

    def _count_heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 4:  # startup + 3 loop iterations
            poller.request_stop()

    poller = _make_poller(
        eligible_span_repository=FakeEligibleSpanRepository(spans=[]),
        checkpoint_connections=connections,
        job_creation_client=FakeJobCreationClient(),
        registry=_registry(),
        poller_interval_seconds=0.01,
        heartbeat_callback=_count_heartbeat,
    )

    with patch("worker.poller.signal.signal"):
        poller.run()

    assert heartbeat_calls == 4
    assert all(connection.closed for connection in connections)


def test_default_heartbeat_callback_is_touch_heartbeat() -> None:
    """Without an explicit override, `Poller` must default to the real
    `worker.heartbeat.touch_heartbeat` -- the same function
    `WorkerRuntime` defaults to and the same file
    `services/worker/Dockerfile`'s single `HEALTHCHECK` instruction checks
    for both processes -- not silently no-op."""
    from worker.heartbeat import touch_heartbeat

    poller = _make_poller(
        eligible_span_repository=FakeEligibleSpanRepository(),
        checkpoint_connections=[],
        job_creation_client=FakeJobCreationClient(),
        registry=_registry(),
    )

    assert poller._heartbeat_callback is touch_heartbeat


# -- stuck-container recovery: heartbeat watchdog ------------------------------


class _RecordingWatchdog:
    instances: list[_RecordingWatchdog] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        _RecordingWatchdog.instances.append(self)

    def start(self) -> None:
        self.started = True


def _run_poller_for_one_tick(**poller_kwargs) -> Poller:
    checkpoint_conn = _checkpoint_connection_with_no_watermark()
    holder: dict[str, Poller] = {}

    def _stop_on_loop_heartbeat() -> None:
        holder["poller"].request_stop()

    poller = _make_poller(
        eligible_span_repository=FakeEligibleSpanRepository(spans=[]),
        checkpoint_connections=[checkpoint_conn],
        job_creation_client=FakeJobCreationClient(),
        registry=_registry(),
        heartbeat_callback=_stop_on_loop_heartbeat,
        **poller_kwargs,
    )
    holder["poller"] = poller
    with patch("worker.poller.signal.signal"):
        poller.run()
    return poller


def test_watchdog_is_off_by_default(monkeypatch) -> None:
    _RecordingWatchdog.instances.clear()
    monkeypatch.setattr("worker.poller.HeartbeatWatchdog", _RecordingWatchdog)

    _run_poller_for_one_tick()

    assert _RecordingWatchdog.instances == []


def test_watchdog_starts_with_the_pollers_stop_event(monkeypatch) -> None:
    _RecordingWatchdog.instances.clear()
    monkeypatch.setattr("worker.poller.HeartbeatWatchdog", _RecordingWatchdog)

    poller = _run_poller_for_one_tick(watchdog_stale_seconds=360.0)

    (watchdog,) = _RecordingWatchdog.instances
    assert watchdog.started
    assert watchdog.kwargs["stale_seconds"] == 360.0
    assert watchdog.kwargs["service"] == "poller"
    assert watchdog.kwargs["stop_event"] is poller._stop_event


def test_heartbeat_refreshes_per_job_creation_call_not_just_per_tick() -> None:
    """A batch makes many sequential HTTP calls; liveness must track that
    progress, or a slow-but-working tick would look stuck to the healthcheck
    and the watchdog and be restarted before it could advance the checkpoint."""
    heartbeat_calls = 0
    checkpoint_conn = _checkpoint_connection_with_no_watermark()
    ingested_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    spans = [_span(ingested_at=ingested_at, span_id=f"{i:016x}") for i in range(1, 4)]
    advance_conn = _FakeConnection()
    advance_conn.queue_result(rowcount=1)  # CAS succeeds
    job_client = FakeJobCreationClient()

    def _count() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1

    poller = _make_poller(
        eligible_span_repository=FakeEligibleSpanRepository(spans=spans),
        checkpoint_connections=[checkpoint_conn, advance_conn],
        job_creation_client=job_client,
        registry=_registry(("relevance", "0.1.0"), ("relevance_embedding", "0.1.0")),
        heartbeat_callback=_count,
    )

    poller.run_one_tick()

    calls_made = len(job_client.calls)
    assert calls_made == 3 * 2  # every span x every installed evaluator
    assert heartbeat_calls == calls_made  # run_one_tick itself adds none of its own
