import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EvaluationJobListResponse, EvaluationJobOut } from "@/lib/api/types";
import { EVALUATION_JOB_LIST_PAGE_SIZE } from "@/lib/evaluationJobFilters";

vi.mock("@/lib/api/browserClient", () => ({
  fetchVigilProxy: vi.fn(),
}));

import { fetchVigilProxy } from "@/lib/api/browserClient";

import { EvaluationJobsExplorer } from "./EvaluationJobsExplorer";

// See EvaluatorConfigForm.test.tsx's identical comment: this mocked module
// is shared across every test in this file.
beforeEach(() => {
  vi.mocked(fetchVigilProxy).mockReset();
});

function job(overrides: Partial<EvaluationJobOut> = {}): EvaluationJobOut {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    trace_id: "4bf92f3577b34da6a3ce929d0e0e4736",
    span_id: "00f067aa0ba902b7",
    evaluator_name: "relevance",
    evaluator_version: "0.1.0",
    status: "pending",
    attempt_count: 0,
    max_retries: 3,
    next_attempt_at: null,
    claimed_at: null,
    claimed_by: null,
    last_error: null,
    created_at: "2026-09-11T12:00:00Z",
    updated_at: "2026-09-11T12:00:00Z",
    ...overrides,
  };
}

const INITIAL_DATA: EvaluationJobListResponse = { jobs: [job()], next_cursor: null };

describe("EvaluationJobsExplorer's evaluator-name filter", () => {
  it("does not refetch on blur when the value hasn't changed", () => {
    render(<EvaluationJobsExplorer initialFilters={{}} initialData={INITIAL_DATA} initialError={null} />);

    const input = screen.getByLabelText(/evaluator name/i);
    fireEvent.focus(input);
    fireEvent.blur(input);

    expect(fetchVigilProxy).not.toHaveBeenCalled();
  });

  it("refetches on blur when the value actually changed", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({ jobs: [job({ evaluator_name: "toxicity" })], next_cursor: null });
    render(<EvaluationJobsExplorer initialFilters={{}} initialData={INITIAL_DATA} initialError={null} />);

    const input = screen.getByLabelText(/evaluator name/i);
    fireEvent.change(input, { target: { value: "toxicity" } });
    fireEvent.blur(input);

    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));
    expect(fetchVigilProxy).toHaveBeenCalledWith("/api/vigil/evaluations/jobs", {
      status: undefined,
      evaluator_name: "toxicity",
      cursor: undefined,
      limit: EVALUATION_JOB_LIST_PAGE_SIZE,
    });
  });

  it("pressing Enter followed by the blur that naturally follows issues only one request", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({ jobs: [job({ evaluator_name: "toxicity" })], next_cursor: null });
    render(<EvaluationJobsExplorer initialFilters={{}} initialData={INITIAL_DATA} initialError={null} />);

    const input = screen.getByLabelText(/evaluator name/i);
    fireEvent.change(input, { target: { value: "toxicity" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));

    fireEvent.blur(input);
    // Give any accidental second call a chance to fire before asserting it didn't.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchVigilProxy).toHaveBeenCalledTimes(1);
  });

  it("still ignores repeated identical values after leading/trailing whitespace is trimmed", () => {
    render(
      <EvaluationJobsExplorer
        initialFilters={{ evaluatorName: "relevance" }}
        initialData={INITIAL_DATA}
        initialError={null}
      />,
    );

    const input = screen.getByLabelText(/evaluator name/i);
    fireEvent.change(input, { target: { value: "  relevance  " } });
    fireEvent.blur(input);

    expect(fetchVigilProxy).not.toHaveBeenCalled();
  });
});
