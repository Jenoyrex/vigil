import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EvaluationJobOut } from "@/lib/api/types";

import { EvaluationJobsTable } from "./EvaluationJobsTable";

const JOB: EvaluationJobOut = {
  id: "11111111-1111-1111-1111-111111111111",
  trace_id: "4bf92f3577b34da6a3ce929d0e0e4736",
  span_id: "00f067aa0ba902b7",
  evaluator_name: "relevance",
  evaluator_version: "0.1.0",
  status: "dead_letter",
  attempt_count: 3,
  max_retries: 3,
  next_attempt_at: null,
  claimed_at: null,
  claimed_by: null,
  last_error: "evaluator raised ValueError",
  created_at: "2026-09-11T12:00:00.000Z",
  updated_at: "2026-09-11T12:00:01.000Z",
};

describe("EvaluationJobsTable", () => {
  it("links the span cell to the job's trace/span, using the existing deep-link query convention", () => {
    render(<EvaluationJobsTable jobs={[JOB]} />);

    const link = screen.getByRole("link");
    expect(link).toHaveAttribute(
      "href",
      `/traces/${JOB.trace_id}?span=${JOB.span_id}&start=${encodeURIComponent(JOB.created_at)}`,
    );
  });

  it("still renders the Copy span ID control alongside the link", () => {
    render(<EvaluationJobsTable jobs={[JOB]} />);
    expect(screen.getByRole("button", { name: /copy span id/i })).toBeInTheDocument();
  });
});
