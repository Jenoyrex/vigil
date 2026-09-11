"""Pure-function tests for app.services.evaluations -- cursor encode/decode
for GET /v1/evaluations/jobs. Mirrors test_query_service.py's cursor test
conventions exactly, adapted for a UUID job id instead of a hex trace_id.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from app.services.evaluations import decode_job_cursor, encode_job_cursor
from app.services.query import QueryValidationError

JOB_ID = uuid.uuid4()


def test_cursor_round_trips_created_at_and_id() -> None:
    created_at = datetime(2026, 9, 11, 12, 0, 0, 123000, tzinfo=UTC)
    cursor = encode_job_cursor(created_at, JOB_ID)
    decoded_created_at, decoded_id = decode_job_cursor(cursor)
    assert decoded_created_at == created_at
    assert decoded_id == JOB_ID


def test_cursor_is_url_safe_base64_text() -> None:
    created_at = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    cursor = encode_job_cursor(created_at, JOB_ID)
    assert isinstance(cursor, str)
    assert "/" not in cursor and "+" not in cursor  # url-safe alphabet


def test_decode_rejects_garbage_cursor() -> None:
    with pytest.raises(QueryValidationError, match="Malformed pagination cursor"):
        decode_job_cursor("not-a-valid-cursor-!!!")


def test_decode_rejects_cursor_with_naive_created_at() -> None:
    payload = json.dumps({"created_at": "2026-09-11T12:00:00", "id": str(JOB_ID)})
    cursor = base64.urlsafe_b64encode(payload.encode()).decode("ascii")
    with pytest.raises(QueryValidationError, match="Malformed pagination cursor"):
        decode_job_cursor(cursor)


def test_decode_rejects_cursor_with_malformed_id() -> None:
    payload = json.dumps({"created_at": "2026-09-11T12:00:00+00:00", "id": "not-a-uuid"})
    cursor = base64.urlsafe_b64encode(payload.encode()).decode("ascii")
    with pytest.raises(QueryValidationError, match="Malformed pagination cursor"):
        decode_job_cursor(cursor)


def test_decode_rejects_cursor_missing_fields() -> None:
    payload = json.dumps({"created_at": "2026-09-11T12:00:00+00:00"})
    cursor = base64.urlsafe_b64encode(payload.encode()).decode("ascii")
    with pytest.raises(QueryValidationError, match="Malformed pagination cursor"):
        decode_job_cursor(cursor)
