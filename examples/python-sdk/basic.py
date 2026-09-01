"""Minimal usage example for the Vigil Python SDK.

Prerequisites:
  1. Run a local Vigil API (see apps/api/README.md for the Postgres +
     ClickHouse setup, then `uv run uvicorn app.main:app --reload`).
  2. Mint a local API key (from apps/api): `uv run python scripts/seed_local_api_key.py`.
     It is printed once -- copy it, never hardcode a real key in source.

    export VIGIL_API_KEY=vgl_...          # from step 2
    export VIGIL_BASE_URL=http://127.0.0.1:8000

    cd packages/sdk-python
    uv run python ../../examples/python-sdk/basic.py
"""

from __future__ import annotations

import os

from vigil import Vigil

vigil = Vigil(
    api_key=os.environ.get("VIGIL_API_KEY", "vgl_replace-with-your-local-dev-key"),
    base_url=os.environ.get("VIGIL_BASE_URL", "http://127.0.0.1:8000"),
    service_name="example-app",
)

# A single span.
with vigil.start_span("example operation") as span:
    span.set_attribute("environment", "demo")
    span.set_input({"message": "hello"})
    span.set_output({"result": "world"})

# Nested spans: an "agent" step containing a "retrieval" and an "llm call".
# Nesting is automatic (via contextvars) -- no manual trace/span-id
# plumbing is needed; a span started inside another's `with` block
# inherits its trace_id and becomes its child.
with vigil.start_span("agent", span_type="agent") as agent:
    with vigil.start_span("retrieval", span_type="retrieval") as retrieval:
        retrieval.set_input({"query": "refund policy"})
        retrieval.set_output({"results": ["doc-1", "doc-42"]})

    with vigil.start_span("llm call", span_type="llm") as llm:
        llm.set_input({"messages": [{"role": "user", "content": "Summarize doc-1"}]})
        llm.set_output({"role": "assistant", "content": "..."})
        llm.record_llm_usage(
            provider="openai",
            model="gpt-4o-mini",
            input_tokens=120,
            output_tokens=48,
            total_tokens=168,
            cost_usd=0.00034,
        )

print(f"trace_id: {agent.trace_id}")

vigil.flush()
vigil.close()
