import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SpanOut } from "@/lib/api/types";

vi.mock("@/lib/api/browserClient", () => ({
  fetchVigilProxy: vi.fn().mockResolvedValue({ results: [] }),
}));

import { SpanDetailPanel } from "./SpanDetailPanel";

const TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736";

function makeSpan(overrides: Partial<SpanOut> = {}): SpanOut {
  return {
    span_id: "00f067aa0ba902b7",
    parent_span_id: null,
    name: "chat.completion",
    span_type: "llm",
    resource: "checkout-service",
    start_time: "2026-09-11T12:00:00Z",
    end_time: "2026-09-11T12:00:01Z",
    duration_ms: 1000,
    status: "ok",
    status_message: null,
    input: null,
    input_size_bytes: 0,
    input_truncated: false,
    output: null,
    output_size_bytes: 0,
    output_truncated: false,
    attributes: {},
    attributes_truncated: false,
    events: [],
    events_truncated: false,
    llm_provider: null,
    llm_model: null,
    llm_input_tokens: null,
    llm_output_tokens: null,
    llm_total_tokens: null,
    llm_cost_usd: null,
    environment: "production",
    release: null,
    ...overrides,
  };
}

describe("SpanDetailPanel", () => {
  it("renders the Evaluations panel for an llm-typed span", () => {
    render(<SpanDetailPanel traceId={TRACE_ID} span={makeSpan({ span_type: "llm" })} />);
    expect(screen.getByText("Evaluations")).toBeInTheDocument();
  });

  it("does not render the Evaluations panel for a non-llm span -- no evaluation is ever possible for it", () => {
    render(<SpanDetailPanel traceId={TRACE_ID} span={makeSpan({ span_type: "tool" })} />);
    expect(screen.queryByText("Evaluations")).not.toBeInTheDocument();
  });
});
