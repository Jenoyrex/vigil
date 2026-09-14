"""Structured (JSON Lines) logging -- Phase 4D, F4. See
worker/logging_config.py's module docstring for the design this proves:

1. every emitted record is valid, single-line JSON with the required
   fields (timestamp/level/service/logger/message);
2. `extra={...}` fields (worker_id, job_id, project_id, evaluator_name,
   etc.) are surfaced as top-level JSON keys, not folded into the message
   string -- genuinely structured, not merely a prettier string;
3. exception info (`logger.exception(...)`) is preserved as a formatted
   traceback, never silently dropped;
4. a value `json.dumps` can't natively handle (e.g. a `uuid.UUID`) is
   stringified rather than raising and breaking logging itself;
5. `configure_logging` installs exactly one handler (idempotently) and
   applies the configured level, with `service` distinguishing `worker`
   from `poller`;
6. `Settings.log_level` validates against real logging level names.

test_runtime.py's existing
`test_orphan_threshold_check_logs_worker_id_count_and_limit` (caplog,
message-text based) is unchanged by this phase -- see worker/runtime.py's
`_check_orphaned_evaluator_threshold`, which keeps its original message
text and additionally attaches the same values as `extra={...}` fields;
this file's `test_runtime_logs_carry_worker_id_as_a_structured_field`
proves the structured side of that same call.
"""

from __future__ import annotations

import json
import logging
import uuid
from unittest.mock import patch

import pytest

from worker.config import Settings
from worker.logging_config import JsonFormatter, configure_logging


def _settings(**overrides) -> Settings:
    return Settings(internal_service_token="test-internal-service-token", **overrides)


# -- JsonFormatter: shape and required fields -------------------------------


def test_emits_valid_single_line_json_with_required_fields() -> None:
    formatter = JsonFormatter(service="worker")
    record = logging.LogRecord(
        name="worker.runtime",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="dispatch tick: claimed=%d outcomes=%s",
        args=(2, {"succeeded": 2}),
        exc_info=None,
    )

    output = formatter.format(record)

    assert "\n" not in output
    payload = json.loads(output)
    assert payload["level"] == "INFO"
    assert payload["service"] == "worker"
    assert payload["logger"] == "worker.runtime"
    assert payload["message"] == "dispatch tick: claimed=2 outcomes={'succeeded': 2}"
    assert payload["timestamp"].endswith("+00:00")


def test_service_field_distinguishes_worker_from_poller() -> None:
    record = logging.LogRecord(
        name="worker.poller",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="evaluation job poller starting",
        args=(),
        exc_info=None,
    )

    worker_payload = json.loads(JsonFormatter(service="worker").format(record))
    poller_payload = json.loads(JsonFormatter(service="poller").format(record))

    assert worker_payload["service"] == "worker"
    assert poller_payload["service"] == "poller"


# -- extra={...} fields are genuine top-level JSON keys ----------------------


def test_extra_fields_are_surfaced_as_top_level_keys() -> None:
    formatter = JsonFormatter(service="worker")
    record = logging.LogRecord(
        name="worker.dispatcher",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Failed to record failure handling for job %s",
        args=("abc",),
        exc_info=None,
    )
    record.job_id = "11111111-1111-1111-1111-111111111111"
    record.evaluator_name = "relevance_embedding"
    record.evaluator_version = "1"

    payload = json.loads(formatter.format(record))
    assert payload["job_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["evaluator_name"] == "relevance_embedding"
    assert payload["evaluator_version"] == "1"


def test_non_json_native_extra_values_are_stringified_not_raised() -> None:
    formatter = JsonFormatter(service="worker")
    record = logging.LogRecord(
        name="worker.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test",
        args=(),
        exc_info=None,
    )
    record.job_id = uuid.uuid4()  # a real, non-JSON-native uuid.UUID object

    output = formatter.format(record)  # must not raise
    payload = json.loads(output)
    assert payload["job_id"] == str(record.job_id)


# -- exception info is preserved, never silently dropped ---------------------


def test_exception_info_is_preserved_as_formatted_traceback() -> None:
    formatter = JsonFormatter(service="worker")
    try:
        raise RuntimeError("evaluator call raised")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="worker.dispatcher",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="claim tick failed; treating as idle",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))
    assert "exception" in payload
    assert "RuntimeError" in payload["exception"]
    assert "evaluator call raised" in payload["exception"]


# -- configure_logging: one handler, correct level, idempotent, per-service -


def test_configure_logging_installs_exactly_one_json_handler() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        configure_logging(service="worker", level="INFO")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.INFO
    finally:
        root.handlers = original_handlers


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        configure_logging(service="worker", level="INFO")
        configure_logging(service="poller", level="DEBUG")
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG
    finally:
        root.handlers = original_handlers


# -- Settings.log_level validation -------------------------------------------


def test_log_level_defaults_to_info() -> None:
    assert _settings().log_level == "INFO"


def test_log_level_is_normalized_to_uppercase() -> None:
    assert _settings(log_level="warning").log_level == "WARNING"


def test_invalid_log_level_raises() -> None:
    with pytest.raises(ValueError, match="not a valid logging level"):
        _settings(log_level="VERBOSE")


# -- real call sites: worker_id/job_id land as structured fields ------------


def test_runtime_logs_carry_worker_id_as_a_structured_field(monkeypatch, caplog) -> None:
    """`worker.runtime.WorkerRuntime.run`'s startup log keeps its original,
    human-readable message text (test_runtime.py's own caplog test already
    covers that) but must also attach worker_id as a genuine `extra` field
    -- proven here by formatting the captured record through the real
    JsonFormatter, the same way production output is produced. Dispatcher/
    connection wiring is irrelevant to this assertion, so both are inert
    placeholders -- `run()` never calls either before this test stops it."""
    import signal

    from worker.runtime import WorkerRuntime

    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)

    runtime = WorkerRuntime(
        dispatcher=object(),
        jobs_connection_factory=lambda: object(),
        worker_id="worker-under-test",
        claim_batch_size=1,
        poll_interval_seconds=0.01,
        reaper_interval_seconds=9999,
        stuck_job_threshold_seconds=9999,
        reaper_batch_size=1,
        heartbeat_callback=lambda: None,
    )

    with (
        patch.object(runtime, "_reap"),
        patch.object(runtime, "_claim_and_dispatch", return_value=False),
        caplog.at_level("INFO"),
    ):
        runtime.request_stop()  # stop immediately after the startup log
        runtime.run()

    formatter = JsonFormatter(service="worker")
    payloads = [json.loads(formatter.format(record)) for record in caplog.records]
    starting = [p for p in payloads if p["message"].startswith("worker runtime starting")]
    assert starting
    assert starting[0]["worker_id"] == "worker-under-test"
