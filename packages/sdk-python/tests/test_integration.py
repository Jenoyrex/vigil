"""Integration test against a real local Vigil API (apps/api).

Mirrors the pattern in apps/api/tests/test_traces_clickhouse_integration.py:
this test skips itself automatically (with an explanatory reason) unless a
local API is actually reachable and a real API key is provided, so it never
blocks the rest of the suite.

To run it:

    cd apps/api
    uv run uvicorn app.main:app --reload
    uv run python scripts/seed_local_api_key.py   # copy the printed key

    cd ../../packages/sdk-python
    VIGIL_SDK_INTEGRATION_API_KEY=<printed key> uv run pytest tests/test_integration.py
"""

from __future__ import annotations

import os

import httpx
import pytest

from vigil import Vigil

_BASE_URL = os.environ.get("VIGIL_SDK_INTEGRATION_BASE_URL", "http://127.0.0.1:8000")
_API_KEY = os.environ.get("VIGIL_SDK_INTEGRATION_API_KEY")


def _local_api_is_reachable() -> bool:
    try:
        response = httpx.get(f"{_BASE_URL}/health", timeout=1.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _API_KEY or not _local_api_is_reachable(),
    reason=(
        "Set VIGIL_SDK_INTEGRATION_API_KEY and run a local Vigil API "
        "(see apps/api/README.md) to run this integration test."
    ),
)


def test_flush_delivers_a_nested_trace_to_the_real_api() -> None:
    vigil = Vigil(api_key=_API_KEY, base_url=_BASE_URL, service_name="sdk-integration-test")
    try:
        with vigil.start_span("agent", span_type="agent") as agent:
            with vigil.start_span("retrieval", span_type="retrieval") as retrieval:
                retrieval.set_attribute("query", "test")
            with vigil.start_span("llm call", span_type="llm") as llm:
                llm.record_llm_usage(provider="openai", model="gpt-4o-mini", total_tokens=10)

        vigil.flush()  # must not raise -- the real API accepted the batch

        assert agent.trace_id == retrieval.trace_id == llm.trace_id
        assert retrieval.parent_span_id == agent.span_id
        assert llm.parent_span_id == agent.span_id
    finally:
        vigil.close()
