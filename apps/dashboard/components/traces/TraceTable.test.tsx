import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TraceSummary } from "@/lib/api/types";

import { TraceTable } from "./TraceTable";

function makeTrace(overrides: Partial<TraceSummary> = {}): TraceSummary {
  return {
    trace_id: "4bf92f3577b34da6a3ce929d0e0e4736",
    start_time: new Date().toISOString(),
    end_time: new Date().toISOString(),
    duration_ms: 1250,
    status: "ok",
    span_count: 4,
    error_span_count: 0,
    root_span_name: "checkout.process_order",
    environment: "production",
    resource: "checkout-service",
    ...overrides,
  };
}

describe("TraceTable", () => {
  it("renders the documented columns: Trace ID, Root Operation, Start Time, Duration, Status, Spans, Environment", () => {
    render(<TraceTable traces={[makeTrace()]} />);
    for (const heading of ["Trace ID", "Root Operation", "Start Time", "Duration", "Status", "Spans", "Environment"]) {
      expect(screen.getByRole("columnheader", { name: heading })).toBeInTheDocument();
    }
  });

  it("truncates the trace id visually while linking with the full id", () => {
    render(<TraceTable traces={[makeTrace()]} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", expect.stringContaining("4bf92f3577b34da6a3ce929d0e0e4736"));
    expect(link.textContent).not.toBe("4bf92f3577b34da6a3ce929d0e0e4736");
    expect(link.textContent?.length).toBeLessThan("4bf92f3577b34da6a3ce929d0e0e4736".length);
  });

  it("shows root_span_name, or an em dash when null", () => {
    const { rerender } = render(<TraceTable traces={[makeTrace({ root_span_name: "my-op" })]} />);
    expect(screen.getByText("my-op")).toBeInTheDocument();

    rerender(<TraceTable traces={[makeTrace({ root_span_name: null })]} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders the status badge text", () => {
    render(<TraceTable traces={[makeTrace({ status: "error" })]} />);
    expect(screen.getByText("error")).toBeInTheDocument();
  });

  it("renders one row per trace", () => {
    render(
      <TraceTable
        traces={[
          makeTrace({ trace_id: "a".repeat(32) }),
          makeTrace({ trace_id: "b".repeat(32) }),
        ]}
      />,
    );
    expect(screen.getAllByRole("row")).toHaveLength(3); // header + 2 data rows
  });
});
