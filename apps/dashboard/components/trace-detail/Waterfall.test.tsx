import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SpanOut } from "@/lib/api/types";
import { buildSpanTree } from "@/lib/tree";

import { Waterfall } from "./Waterfall";

function makeSpan(overrides: Partial<SpanOut> & Pick<SpanOut, "span_id">): SpanOut {
  return {
    parent_span_id: null,
    name: `span-${overrides.span_id}`,
    span_type: "function",
    resource: "test-service",
    start_time: "2026-08-31T12:00:00.000Z",
    end_time: "2026-08-31T12:00:01.000Z",
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

const TRACE_START_MS = new Date("2026-08-31T12:00:00.000Z").getTime();

describe("Waterfall", () => {
  it("renders one row per span, in depth-first order", () => {
    const spans = [
      makeSpan({ span_id: "root", parent_span_id: null, name: "agent" }),
      makeSpan({ span_id: "child", parent_span_id: "root", name: "llm-call", start_time: "2026-08-31T12:00:00.100Z" }),
    ];
    const roots = buildSpanTree(spans);
    render(
      <Waterfall roots={roots} traceStartMs={TRACE_START_MS} traceDurationMs={1000} selectedSpanId={null} onSelect={() => {}} />,
    );
    expect(screen.getByText("agent")).toBeInTheDocument();
    expect(screen.getByText("llm-call")).toBeInTheDocument();
  });

  it("calls onSelect with the span_id when a row is clicked", () => {
    const spans = [makeSpan({ span_id: "root", parent_span_id: null, name: "agent" })];
    const roots = buildSpanTree(spans);
    const onSelect = vi.fn();
    render(
      <Waterfall roots={roots} traceStartMs={TRACE_START_MS} traceDurationMs={1000} selectedSpanId={null} onSelect={onSelect} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /agent/ }));
    expect(onSelect).toHaveBeenCalledWith("root");
  });

  it("marks the selected span's row as pressed", () => {
    const spans = [makeSpan({ span_id: "root", parent_span_id: null, name: "agent" })];
    const roots = buildSpanTree(spans);
    render(
      <Waterfall roots={roots} traceStartMs={TRACE_START_MS} traceDurationMs={1000} selectedSpanId="root" onSelect={() => {}} />,
    );
    expect(screen.getByRole("button", { name: /agent/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("indicates a span with a missing parent (truncated trace)", () => {
    const spans = [makeSpan({ span_id: "orphan", parent_span_id: "not-loaded", name: "mystery-span" })];
    const roots = buildSpanTree(spans);
    render(
      <Waterfall roots={roots} traceStartMs={TRACE_START_MS} traceDurationMs={1000} selectedSpanId={null} onSelect={() => {}} />,
    );
    expect(screen.getByTitle(/parent span not loaded/i)).toBeInTheDocument();
  });

  it("renders every span even when the tree is empty at the root but has no spans", () => {
    render(<Waterfall roots={[]} traceStartMs={TRACE_START_MS} traceDurationMs={1000} selectedSpanId={null} onSelect={() => {}} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
