"""Evaluation job poller -- Phase 3H,
docs/decisions/005-evaluation-job-storage-worker.md's Phase 3H amendment.

Discovers ClickHouse `spans` newly eligible for evaluation and, for each
one, asks `apps/api`'s job-creation endpoint (`POST /v1/evaluations/jobs`)
whether a job should exist, once per `(span, installed evaluator)` pair --
`services/worker`'s installed `EvaluatorRegistry` is the only source of
which evaluators to even ask about (a Python-level, code-installed fact,
never a database-driven notion of "what's enabled" -- that business
decision belongs to `apps/api`, ADR 005 section 10, and this module never
duplicates it).

**Ownership boundary, unchanged from the approved design**: this module
owns ClickHouse span discovery, checkpoint lifecycle, calling the API, and
poller scheduling -- nothing else. It never decides whether a job *should*
exist (`apps/api`'s job); it never claims, dispatches, evaluates, retries,
or reaps a job (`worker.runtime.WorkerRuntime`/`worker.dispatcher.Dispatcher`/
`worker.failure_handling`/`worker.reaper`'s jobs, all untouched by this
phase).

**Checkpoint/watermark strategy -- overlap window + idempotent insertion**:
`evaluation_poller_checkpoint`'s single `last_ingested_at` column is
unchanged (no schema addition). Every tick queries
`ingested_at > (checkpoint - poller_overlap_seconds)` -- a deliberate,
constant trailing re-scan on every single tick, not a one-time correction --
because ClickHouse's `ingested_at DEFAULT now64(3)` is evaluated once per
`INSERT` statement, not once per row, so any multi-span ingestion batch
produces identical `ingested_at` values across all its rows: same-timestamp
ties are the common case here, not an edge case, and a truncated batch
naively advancing to `MAX(ingested_at)` would silently drop whatever shares
that exact timestamp but didn't make it into this tick's `LIMIT`. Re-scanning
the overlap window also covers ClickHouse's own insert-visibility lag (a row
becoming query-visible after a later-`ingested_at` row already advanced the
checkpoint past it). Both risks are made safe, not by precise exactly-once
discovery, but by the existing `evaluation_jobs` unique constraint + `apps/api`'s
idempotent "return the existing row" handling: at-least-once discovery,
idempotent creation. See `worker/clickhouse/eligible_span_repository.py`'s
module docstring for why the alternative (a stored composite cursor) was
rejected in the approved design.

**Checkpoint advancement is all-or-nothing per tick**: the checkpoint only
advances if *every* `(span, evaluator)` pair scanned this tick received a
definitive outcome from the job-creation call -- `created`, `already_exists`,
`not_enabled`, or `not_sampled` (the four `EvaluationJobCreateResponse.reason`
values `apps/api` can return). A transport-level failure (connection
refused, timeout, unexpected exception) or an unexpected HTTP status
(including `404`, conservatively -- not one of the four literal "definitive"
outcomes) leaves the tick's whole batch unresolved: the checkpoint does not
move, and the next tick re-scans the identical (overlap-widened) window,
safely, because every already-resolved call in it is now a free idempotent
no-op.

**Concurrency**: multiple poller processes may run simultaneously, safely,
with no distributed lock -- see `worker/postgres/poller_checkpoint_repository.py`'s
optimistic-CAS `advance_checkpoint`. V1 operationally expects exactly one
poller process; running more is a documented, tested, but not load-bearing
capability.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

import psycopg

from worker.clickhouse.eligible_span_repository import EligibleSpan, EligibleSpanRepository
from worker.heartbeat import touch_heartbeat
from worker.postgres.poller_checkpoint_repository import PollerCheckpointRepository
from worker.registry import EvaluatorRegistry

logger = logging.getLogger(__name__)

#: The exact four reasons apps/api's POST /v1/evaluations/jobs can return
#: that count as "this candidate is resolved, whatever the answer was" --
#: see this module's own docstring on why 404 is deliberately NOT included
#: (treated the same as a transport failure: conservative, not one of the
#: four literal definitive outcomes the approved design names).
_DEFINITIVE_REASONS = frozenset({"created", "already_exists", "not_enabled", "not_sampled"})


@dataclass(frozen=True)
class JobCreationOutcome:
    """One `(span, evaluator)` pair's result from calling
    `POST /v1/evaluations/jobs`. `resolved=True` only for one of the four
    literal definitive reasons in `_DEFINITIVE_REASONS` -- a genuine,
    actionable answer. `resolved=False` for everything else: a transport
    failure (no response was ever received), an unexpected status code, or
    a `404` (span not found / wrong project) -- all treated identically for
    checkpoint-advancement purposes, per this module's docstring.
    """

    resolved: bool
    reason: str | None = None
    job_id: UUID | None = None


#: (project_id, trace_id, span_id, evaluator_name, evaluator_version) -> outcome.
JobCreationClient = Callable[[UUID, str, str, str, str], JobCreationOutcome]

#: A fresh `psycopg.Connection` per call -- worker.postgres.client.get_connection's
#: exact shape, injected rather than imported directly (mirrors
#: worker.runtime.WorkerRuntime's identical jobs_connection_factory
#: injection point), so tests can supply a fake connection factory instead
#: of opening a real one.
CheckpointConnectionFactory = Callable[[], psycopg.Connection]


class HttpJobCreationClient:
    """Production `JobCreationClient`: `POST /v1/evaluations/jobs` over
    HTTP, authenticated via `X-Vigil-Internal-Token`. Uses the stdlib
    `urllib` rather than adding a new external HTTP-client dependency to
    `services/worker/pyproject.toml` for this one, simple, synchronous
    call shape (one JSON body in, one JSON body out, one custom header).
    """

    def __init__(
        self, *, base_url: str, internal_service_token: str, timeout_seconds: float
    ) -> None:
        self._url = base_url.rstrip("/") + "/v1/evaluations/jobs"
        self._token = internal_service_token
        self._timeout_seconds = timeout_seconds

    def __call__(
        self,
        project_id: UUID,
        trace_id: str,
        span_id: str,
        evaluator_name: str,
        evaluator_version: str,
    ) -> JobCreationOutcome:
        body = json.dumps(
            {
                "project_id": str(project_id),
                "trace_id": trace_id,
                "span_id": span_id,
                "evaluator_name": evaluator_name,
                "evaluator_version": evaluator_version,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Vigil-Internal-Token": self._token,
            },
        )
        extra = {
            "project_id": str(project_id),
            "span_id": span_id,
            "evaluator_name": evaluator_name,
            "evaluator_version": evaluator_version,
        }
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                logger.warning(
                    "job-creation call returned 404 (span not found / wrong project) "
                    "project_id=%s span_id=%s evaluator=%s/%s -- not treated as resolved",
                    project_id,
                    span_id,
                    evaluator_name,
                    evaluator_version,
                    extra=extra,
                )
            else:
                logger.error(
                    "job-creation call failed status=%s project_id=%s span_id=%s evaluator=%s/%s",
                    exc.code,
                    project_id,
                    span_id,
                    evaluator_name,
                    evaluator_version,
                    extra={**extra, "status": exc.code},
                )
            return JobCreationOutcome(resolved=False)
        except urllib.error.URLError as exc:
            logger.error(
                "job-creation call unreachable project_id=%s span_id=%s evaluator=%s/%s: %s",
                project_id,
                span_id,
                evaluator_name,
                evaluator_version,
                exc,
                extra={**extra, "error": str(exc)},
            )
            return JobCreationOutcome(resolved=False)

        reason = payload.get("reason")
        if reason not in _DEFINITIVE_REASONS:
            logger.error(
                "job-creation call returned an unrecognized reason=%r project_id=%s span_id=%s "
                "evaluator=%s/%s",
                reason,
                project_id,
                span_id,
                evaluator_name,
                evaluator_version,
                extra={**extra, "reason": reason},
            )
            return JobCreationOutcome(resolved=False)

        job_id = UUID(payload["job_id"]) if payload.get("job_id") else None
        return JobCreationOutcome(resolved=True, reason=reason, job_id=job_id)


class Poller:
    """Constructed once per process with an already-built
    `EligibleSpanRepository`, a `checkpoint_connection_factory`, a
    `job_creation_client`, and the worker's `EvaluatorRegistry`; `run()`
    loops until a shutdown signal is received, mirroring
    `worker.runtime.WorkerRuntime`'s own shape (signal handling,
    interruptible idle wait via `threading.Event`) without importing or
    modifying that module at all -- this is a fully independent process.
    """

    def __init__(
        self,
        *,
        eligible_span_repository: EligibleSpanRepository,
        checkpoint_connection_factory: CheckpointConnectionFactory,
        job_creation_client: JobCreationClient,
        registry: EvaluatorRegistry,
        poller_batch_size: int,
        poller_overlap_seconds: float,
        poller_start_time_lookback_days: int,
        poller_interval_seconds: float,
        heartbeat_callback: Callable[[], None] = touch_heartbeat,
    ) -> None:
        self._eligible_span_repository = eligible_span_repository
        self._checkpoint_connection_factory = checkpoint_connection_factory
        self._job_creation_client = job_creation_client
        self._registry = registry
        self._poller_batch_size = poller_batch_size
        self._poller_overlap_seconds = poller_overlap_seconds
        self._poller_start_time_lookback_days = poller_start_time_lookback_days
        self._poller_interval_seconds = poller_interval_seconds
        self._heartbeat_callback = heartbeat_callback
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Signal-handler-safe: only ever sets an already-constructed
        `threading.Event`, never touches a database or network connection.
        See `_install_signal_handlers`.
        """
        self._stop_event.set()

    def run(self) -> None:
        """Must be called from the process's main thread -- `signal.signal`
        only works from the main thread in Python, the identical
        constraint `worker.runtime.WorkerRuntime.run` documents for the
        identical reason.

        `self._heartbeat_callback()` (Phase 4D, F6 -- `worker.heartbeat
        .touch_heartbeat` by default, the same one `WorkerRuntime.run`
        uses and the same file `services/worker/Dockerfile`'s single
        `HEALTHCHECK` instruction checks for both processes) is called
        once at startup and once at the top of every loop iteration --
        never inside `run_one_tick()` itself, so its own cost is never
        what a shutdown check waits behind. `run_one_tick()`'s own
        ClickHouse scan, PostgreSQL checkpoint read/write
        (`database_timeout_seconds`), and HTTP call to apps/api
        (`poller_job_creation_timeout_seconds`) are all already
        individually time-bounded, so a heartbeat that stops refreshing
        for longer than a generous multiple of those bounds is a genuine
        stuck-loop signal.
        """
        self._install_signal_handlers()
        logger.info("evaluation job poller starting")
        self._heartbeat_callback()
        while not self._stop_event.is_set():
            self._heartbeat_callback()
            self.run_one_tick()
            if self._stop_event.is_set():
                break
            self._stop_event.wait(self._poller_interval_seconds)
        logger.info("evaluation job poller stopped")

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object) -> None:
        logger.info(
            "evaluation job poller received signal %s, requesting shutdown",
            signum,
            extra={"signal": signum},
        )
        self.request_stop()

    def run_one_tick(self) -> None:
        """One full poll tick: read the checkpoint, scan a bounded
        ClickHouse batch, call job-creation for every `(span, evaluator)`
        pair in it, and advance the checkpoint only if every one of those
        calls resolved definitively. Never raises: every failure mode is
        caught, logged, and left for the next tick to retry -- matching
        `WorkerRuntime`'s own per-tick isolation discipline.
        """
        checkpoint_read_succeeded, observed_watermark = self._read_checkpoint()
        if not checkpoint_read_succeeded:
            return

        lower_bound, start_date_hint = _compute_scan_window(
            observed_watermark,
            overlap_seconds=self._poller_overlap_seconds,
            lookback_days=self._poller_start_time_lookback_days,
        )

        try:
            batch = self._eligible_span_repository.select_eligible_spans(
                lower_bound=lower_bound,
                start_date_hint=start_date_hint,
                batch_size=self._poller_batch_size,
            )
        except Exception:
            logger.exception("poller tick failed scanning ClickHouse; will retry next tick")
            return

        if not batch:
            logger.debug("poller tick found no eligible spans; not advancing checkpoint")
            return

        installed_evaluators = sorted(self._registry.registered_keys())
        all_resolved = True
        max_ingested_at = observed_watermark

        for span in batch:
            for evaluator_name, evaluator_version in installed_evaluators:
                outcome = self._call_job_creation(span, evaluator_name, evaluator_version)
                if not outcome.resolved:
                    all_resolved = False
            if max_ingested_at is None or span.ingested_at > max_ingested_at:
                max_ingested_at = span.ingested_at

        if not all_resolved:
            logger.warning(
                "poller tick had unresolved job-creation calls (scanned=%d); "
                "checkpoint not advanced",
                len(batch),
                extra={"scanned": len(batch)},
            )
            return

        self._advance_checkpoint(
            observed_watermark=observed_watermark, new_watermark=max_ingested_at
        )

    def _call_job_creation(
        self, span: EligibleSpan, evaluator_name: str, evaluator_version: str
    ) -> JobCreationOutcome:
        try:
            return self._job_creation_client(
                span.project_id, span.trace_id, span.span_id, evaluator_name, evaluator_version
            )
        except Exception:
            logger.exception(
                "job-creation call raised for span_id=%s evaluator=%s/%s",
                span.span_id,
                evaluator_name,
                evaluator_version,
                extra={
                    "project_id": str(span.project_id),
                    "span_id": span.span_id,
                    "evaluator_name": evaluator_name,
                    "evaluator_version": evaluator_version,
                },
            )
            return JobCreationOutcome(resolved=False)

    def _read_checkpoint(self) -> tuple[bool, datetime | None]:
        """Returns `(succeeded, watermark)` -- `succeeded=False` (checkpoint
        read/connection failure, already logged) is distinct from a
        legitimate `(True, None)` ("no checkpoint yet"), which a bare
        `datetime | None` return can't express unambiguously.
        """
        try:
            connection = self._checkpoint_connection_factory()
            try:
                return True, PollerCheckpointRepository(connection).get_or_create_checkpoint()
            finally:
                connection.close()
        except Exception:
            logger.exception("poller tick failed reading checkpoint; will retry next tick")
            return False, None

    def _advance_checkpoint(
        self, *, observed_watermark: datetime | None, new_watermark: datetime
    ) -> None:
        try:
            connection = self._checkpoint_connection_factory()
            try:
                advanced = PollerCheckpointRepository(connection).advance_checkpoint(
                    observed_watermark=observed_watermark, new_watermark=new_watermark
                )
            finally:
                connection.close()
        except Exception:
            logger.exception("poller tick failed advancing checkpoint; will retry next tick")
            return

        if advanced:
            logger.info("poller checkpoint advanced to %s", new_watermark)
        else:
            logger.info(
                "poller checkpoint advance lost a race to another poller instance "
                "(observed_watermark=%s) -- not treated as an error",
                observed_watermark,
            )


def _compute_scan_window(
    observed_watermark: datetime | None, *, overlap_seconds: float, lookback_days: int
) -> tuple[datetime | None, date | None]:
    """`lower_bound`: `None` on the very first poll (no checkpoint yet --
    scan the whole retention window), else `observed_watermark -
    overlap_seconds` (the overlap-window re-scan, see module docstring).
    `start_date_hint`: `None` on the very first poll (correctly matching
    `evaluation_poller_checkpoint`'s "NULL means start from the beginning
    of the retention window" semantics -- applying a lookback-day hint
    derived from "today" instead would silently prune out any real backlog
    older than that on a fresh deployment), else derived from the
    checkpoint's own date, never from wall-clock "today".
    """
    if observed_watermark is None:
        return None, None
    lower_bound = observed_watermark - timedelta(seconds=overlap_seconds)
    start_date_hint = observed_watermark.date() - timedelta(days=lookback_days)
    return lower_bound, start_date_hint


if __name__ == "__main__":
    # Supports `python -m worker.poller` directly, delegating to
    # worker/poller_main.py's actual settings/dependency wiring -- kept
    # separate so this module stays primarily the tick/orchestration logic,
    # matching worker/runtime.py + worker/__main__.py's own split.
    from worker.poller_main import main

    main()
