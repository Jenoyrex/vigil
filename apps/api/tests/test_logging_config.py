"""Structured (JSON Lines) logging -- Phase 4D, F4. See
app/logging_config.py's module docstring for the design this proves:

1. every emitted record is valid, single-line JSON with the required fields
   (timestamp/level/service/logger/message);
2. `extra={...}` fields are surfaced as top-level JSON keys, not folded
   into the message string -- genuinely structured, not merely a prettier
   string;
3. exception info (`logger.exception(...)`) is preserved as a formatted
   traceback, never silently dropped;
4. a value `json.dumps` can't natively handle (a UUID, an arbitrary object)
   is stringified rather than raising and breaking logging itself;
5. `configure_logging` installs exactly one handler (idempotently) and
   applies the configured level;
6. `Settings.log_level` validates against real logging level names.

`app/logging_config.py`'s request-id contextvar helpers themselves are
covered by test_request_id_middleware.py (full request lifecycle, via
TestClient) rather than here.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.config import Settings
from app.logging_config import JsonFormatter, configure_logging


def _settings(**overrides) -> Settings:
    return Settings(internal_service_token="test-internal-service-token", **overrides)


# -- JsonFormatter: shape and required fields -------------------------------


def test_emits_valid_single_line_json_with_required_fields() -> None:
    formatter = JsonFormatter(service="api")
    record = logging.LogRecord(
        name="app.api.v1.traces",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ingested spans",
        args=(),
        exc_info=None,
    )

    output = formatter.format(record)

    assert "\n" not in output
    payload = json.loads(output)
    assert payload["level"] == "INFO"
    assert payload["service"] == "api"
    assert payload["logger"] == "app.api.v1.traces"
    assert payload["message"] == "ingested spans"
    assert "timestamp" in payload
    # ISO 8601, UTC, millisecond precision, unambiguous timezone offset.
    assert payload["timestamp"].endswith("+00:00")


def test_message_formatting_uses_percent_style_args() -> None:
    formatter = JsonFormatter(service="api")
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="span_count=%d",
        args=(7,),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))
    assert payload["message"] == "span_count=7"


# -- extra={...} fields are genuine top-level JSON keys ----------------------


def test_extra_fields_are_surfaced_as_top_level_keys() -> None:
    formatter = JsonFormatter(service="api")
    record = logging.LogRecord(
        name="app.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="ClickHouse unavailable during trace list",
        args=(),
        exc_info=None,
    )
    record.project_id = "11111111-1111-1111-1111-111111111111"
    record.error = "connection refused"

    payload = json.loads(formatter.format(record))
    assert payload["project_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["error"] == "connection refused"
    # Not duplicated into the message string -- structured fields are a
    # separate channel, not a re-encoding of the human-readable text.
    assert "project_id" not in payload["message"]


def test_non_json_native_extra_values_are_stringified_not_raised() -> None:
    """A value json.dumps can't natively serialize (here, a plain object)
    must never crash logging itself -- `default=str` in
    JsonFormatter.format is what guarantees that."""
    formatter = JsonFormatter(service="api")
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test",
        args=(),
        exc_info=None,
    )
    record.weird = object()

    output = formatter.format(record)  # must not raise
    payload = json.loads(output)
    assert "weird" in payload


# -- exception info is preserved, never silently dropped ---------------------


def test_exception_info_is_preserved_as_formatted_traceback() -> None:
    formatter = JsonFormatter(service="api")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="job-creation call raised",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))
    assert "exception" in payload
    assert "ValueError" in payload["exception"]
    assert "boom" in payload["exception"]


def test_no_exception_field_when_no_exception_info() -> None:
    formatter = JsonFormatter(service="api")
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ok",
        args=(),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))
    assert "exception" not in payload


# -- configure_logging: one handler, correct level, idempotent --------------


def test_configure_logging_installs_exactly_one_json_handler() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        configure_logging(service="api", level="INFO")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.INFO
    finally:
        root.handlers = original_handlers


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        configure_logging(service="api", level="INFO")
        configure_logging(service="api", level="DEBUG")
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG
    finally:
        root.handlers = original_handlers


# -- Settings.log_level validation -------------------------------------------


def test_log_level_defaults_to_info() -> None:
    assert _settings().log_level == "INFO"


def test_log_level_is_normalized_to_uppercase() -> None:
    assert _settings(log_level="debug").log_level == "DEBUG"


def test_invalid_log_level_raises() -> None:
    with pytest.raises(ValueError, match="not a valid logging level"):
        _settings(log_level="VERBOSE")
