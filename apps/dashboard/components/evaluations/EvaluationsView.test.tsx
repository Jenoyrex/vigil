import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EvaluationJobListResponse, EvaluationJobOut } from "@/lib/api/types";

vi.mock("@/lib/api/browserClient", () => ({
  fetchVigilProxy: vi.fn(),
}));

import { fetchVigilProxy } from "@/lib/api/browserClient";

import { EvaluationsView } from "./EvaluationsView";

beforeEach(() => {
  vi.mocked(fetchVigilProxy).mockReset();
  window.history.replaceState(null, "", "/evaluations");
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

const INITIAL_JOBS: EvaluationJobListResponse = { jobs: [job()], next_cursor: null };

/** Both panels are always mounted (see EvaluationsView's own docstring), so
 * EvaluatorConfigsPanel's "add a new evaluator" input shares the exact
 * label text "Evaluator name" with the Jobs filter input -- getByLabelText
 * alone is ambiguous. This targets the Jobs tab's filter input specifically
 * by its known id. */
function evaluatorNameFilterInput(): HTMLElement {
  const input = document.getElementById("job-evaluator-filter");
  if (!input) throw new Error("#job-evaluator-filter not found");
  return input;
}

describe("EvaluationsView tab switching", () => {
  it("keeps the Jobs panel's filtered data after switching away and back to it -- never resetting to server-rendered initial data", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({ jobs: [job({ evaluator_name: "toxicity" })], next_cursor: null });

    render(
      <EvaluationsView
        initialTab="jobs"
        initialConfigs={[]}
        initialConfigsError={null}
        initialJobFilters={{}}
        initialJobs={INITIAL_JOBS}
        initialJobsError={null}
      />,
    );

    fireEvent.change(evaluatorNameFilterInput(), { target: { value: "toxicity" } });
    fireEvent.blur(evaluatorNameFilterInput());
    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("toxicity")).toBeInTheDocument();
    expect(screen.queryByText("relevance")).not.toBeInTheDocument();

    // Switch to Configuration, then back to Jobs.
    fireEvent.click(screen.getByRole("tab", { name: "Configuration" }));
    fireEvent.click(screen.getByRole("tab", { name: "Jobs" }));

    // The filtered result must still be showing -- not reverted to the
    // original server-rendered "relevance" job -- and no extra fetch
    // should have been triggered merely by switching tabs.
    expect(screen.getByText("toxicity")).toBeInTheDocument();
    expect(screen.queryByText("relevance")).not.toBeInTheDocument();
    expect(fetchVigilProxy).toHaveBeenCalledTimes(1);
  });

  it("hides the inactive panel via the native `hidden` attribute rather than unmounting it", () => {
    render(
      <EvaluationsView
        initialTab="config"
        initialConfigs={[]}
        initialConfigsError={null}
        initialJobFilters={{}}
        initialJobs={INITIAL_JOBS}
        initialJobsError={null}
      />,
    );

    const jobsPanel = document.getElementById("evaluations-panel-jobs");
    const configPanel = document.getElementById("evaluations-panel-config");
    expect(jobsPanel).toHaveProperty("hidden", true);
    expect(configPanel).toHaveProperty("hidden", false);
    // Its content is mounted in the DOM regardless of being hidden -- not
    // removed and re-created on the next switch.
    expect(jobsPanel?.querySelector("#job-evaluator-filter")).not.toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "Jobs" }));
    expect(jobsPanel).toHaveProperty("hidden", false);
    expect(configPanel).toHaveProperty("hidden", true);
  });

  it("syncs the selected tab into the URL, and switching back restores it", () => {
    render(
      <EvaluationsView
        initialTab="config"
        initialConfigs={[]}
        initialConfigsError={null}
        initialJobFilters={{}}
        initialJobs={INITIAL_JOBS}
        initialJobsError={null}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Jobs" }));
    expect(window.location.pathname).toBe("/evaluations");
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("jobs");

    fireEvent.click(screen.getByRole("tab", { name: "Configuration" }));
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("config");
  });

  it("preserves a Jobs filter already applied to the URL when switching tabs and back", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({ jobs: [job({ evaluator_name: "toxicity" })], next_cursor: null });

    render(
      <EvaluationsView
        initialTab="jobs"
        initialConfigs={[]}
        initialConfigsError={null}
        initialJobFilters={{}}
        initialJobs={INITIAL_JOBS}
        initialJobsError={null}
      />,
    );

    fireEvent.change(evaluatorNameFilterInput(), { target: { value: "toxicity" } });
    fireEvent.blur(evaluatorNameFilterInput());
    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));
    expect(new URLSearchParams(window.location.search).get("evaluator_name")).toBe("toxicity");

    fireEvent.click(screen.getByRole("tab", { name: "Configuration" }));
    expect(new URLSearchParams(window.location.search).get("evaluator_name")).toBe("toxicity");
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("config");

    fireEvent.click(screen.getByRole("tab", { name: "Jobs" }));
    expect(new URLSearchParams(window.location.search).get("evaluator_name")).toBe("toxicity");
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("jobs");
  });
});
