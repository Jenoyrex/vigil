"""Pure unit tests for app.services.query: time-window resolution, cursor
encode/decode, status derivation. No FastAPI, no ClickHouse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.query import (
    QueryValidationError,
    _as_utc,
    _derive_status,
    decode_trace_cursor,
    encode_trace_cursor,
    resolve_time_window,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


# -- resolve_time_window -----------------------------------------------


def test_omitted_bounds_default_to_previous_24_hours() -> None:
    window = resolve_time_window(None, None, default_window_hours=24, max_window_days=7)
    assert window.start_time_to - window.start_time_from == timedelta(hours=24)


def test_omitted_start_defaults_relative_to_supplied_end() -> None:
    end = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
    window = resolve_time_window(None, end, default_window_hours=24, max_window_days=7)
    assert window.start_time_from == end - timedelta(hours=24)
    assert window.start_time_to == end


def test_omitted_end_defaults_to_now() -> None:
    start = datetime.now(UTC) - timedelta(hours=1)
    window = resolve_time_window(start, None, default_window_hours=24, max_window_days=7)
    assert (datetime.now(UTC) - window.start_time_to) < timedelta(seconds=5)


def test_window_within_max_is_accepted() -> None:
    start = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=7)
    window = resolve_time_window(start, end, default_window_hours=24, max_window_days=7)
    assert window.start_time_from == start
    assert window.start_time_to == end


def test_window_wider_than_max_is_rejected() -> None:
    start = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=7, seconds=1)
    with pytest.raises(QueryValidationError, match="must not exceed 7 days"):
        resolve_time_window(start, end, default_window_hours=24, max_window_days=7)


def test_start_after_end_is_rejected() -> None:
    start = datetime(2026, 8, 31, 0, 0, 0, tzinfo=UTC)
    end = start - timedelta(hours=1)
    with pytest.raises(QueryValidationError, match="must not be after"):
        resolve_time_window(start, end, default_window_hours=24, max_window_days=7)


def test_start_equal_to_end_is_accepted() -> None:
    moment = datetime(2026, 8, 31, 0, 0, 0, tzinfo=UTC)
    window = resolve_time_window(moment, moment, default_window_hours=24, max_window_days=7)
    assert window.start_time_from == window.start_time_to == moment


# -- cursor encode/decode ------------------------------------------------


def test_cursor_round_trips_start_time_and_trace_id() -> None:
    start_time = datetime(2026, 8, 31, 12, 0, 0, 123000, tzinfo=UTC)
    cursor = encode_trace_cursor(start_time, TRACE_ID)
    decoded_time, decoded_trace_id = decode_trace_cursor(cursor)
    assert decoded_time == start_time
    assert decoded_trace_id == TRACE_ID


def test_cursor_is_url_safe_base64_text() -> None:
    start_time = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
    cursor = encode_trace_cursor(start_time, TRACE_ID)
    assert isinstance(cursor, str)
    assert "/" not in cursor and "+" not in cursor  # url-safe alphabet


def test_decode_rejects_garbage_cursor() -> None:
    with pytest.raises(QueryValidationError, match="Malformed pagination cursor"):
        decode_trace_cursor("not-a-valid-cursor-!!!")


def test_decode_rejects_cursor_with_naive_start_time() -> None:
    import base64
    import json

    payload = json.dumps({"start_time": "2026-08-31T12:00:00", "trace_id": TRACE_ID})
    cursor = base64.urlsafe_b64encode(payload.encode()).decode("ascii")
    with pytest.raises(QueryValidationError, match="Malformed pagination cursor"):
        decode_trace_cursor(cursor)


def test_decode_rejects_cursor_with_malformed_trace_id() -> None:
    import base64
    import json

    payload = json.dumps({"start_time": "2026-08-31T12:00:00+00:00", "trace_id": "not-hex"})
    cursor = base64.urlsafe_b64encode(payload.encode()).decode("ascii")
    with pytest.raises(QueryValidationError, match="Malformed pagination cursor"):
        decode_trace_cursor(cursor)


def test_decode_rejects_cursor_missing_fields() -> None:
    import base64
    import json

    payload = json.dumps({"start_time": "2026-08-31T12:00:00+00:00"})
    cursor = base64.urlsafe_b64encode(payload.encode()).decode("ascii")
    with pytest.raises(QueryValidationError, match="Malformed pagination cursor"):
        decode_trace_cursor(cursor)


# -- status derivation ----------------------------------------------------


def test_status_is_error_when_any_error_span_exists() -> None:
    assert _derive_status(error_count=1, root_count=1) == "error"
    assert _derive_status(error_count=1, root_count=0) == "error"


def test_status_is_ok_when_root_span_present_and_no_errors() -> None:
    assert _derive_status(error_count=0, root_count=1) == "ok"


def test_status_is_unknown_when_no_root_span_and_no_errors() -> None:
    assert _derive_status(error_count=0, root_count=0) == "unknown"


# -- naive-datetime normalization -----------------------------------------


def test_as_utc_attaches_utc_to_naive_datetime() -> None:
    naive = datetime(2026, 8, 31, 12, 0, 0)
    result = _as_utc(naive)
    assert result.tzinfo == UTC
    assert result.replace(tzinfo=None) == naive


def test_as_utc_leaves_aware_datetime_unchanged() -> None:
    aware = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
    assert _as_utc(aware) == aware
