import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EvaluatorConfigOut } from "@/lib/api/types";

vi.mock("@/lib/api/browserClient", () => ({
  fetchVigilProxy: vi.fn(),
}));

import { fetchVigilProxy } from "@/lib/api/browserClient";

import { EvaluatorConfigsPanel } from "./EvaluatorConfigsPanel";

// See EvaluatorConfigForm.test.tsx's identical comment: this mocked module
// is shared across every test in this file, including by the real
// (unmocked) EvaluatorConfigForm children this panel renders.
beforeEach(() => {
  vi.mocked(fetchVigilProxy).mockReset();
});

const RELEVANCE_CONFIG: EvaluatorConfigOut = {
  evaluator_name: "relevance",
  enabled: true,
  sampling_rate: 0.25,
  threshold: 0.42,
  max_retries: 5,
  created_at: "2026-09-11T12:00:00Z",
  updated_at: "2026-09-11T12:00:00Z",
};

describe("EvaluatorConfigsPanel", () => {
  it("does not discard an in-progress add-evaluator draft when an unrelated existing config is saved", async () => {
    // A slightly longer timeout than the default 5000ms: this test does
    // more render/interaction cycles than the others in this file and has
    // been observed to occasionally exceed it under slow I/O.
    vi.mocked(fetchVigilProxy).mockResolvedValue({ ...RELEVANCE_CONFIG, sampling_rate: 0.5 });
    render(<EvaluatorConfigsPanel initialConfigs={[RELEVANCE_CONFIG]} initialError={null} />);

    // Start (but do not submit) a second evaluator's config.
    fireEvent.change(screen.getByLabelText(/evaluator name/i), { target: { value: "toxicity" } });
    fireEvent.click(screen.getByRole("button", { name: /configure/i }));
    expect(screen.getByText("toxicity")).toBeInTheDocument();

    // Save the unrelated, already-existing "relevance" config -- its form
    // is rendered before the add-form's, so it's the first "Save" button.
    const [relevanceSaveButton] = screen.getAllByRole("button", { name: /^save$/i });
    fireEvent.click(relevanceSaveButton);
    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));

    // The in-progress "toxicity" add-form must still be there.
    expect(screen.getByText("toxicity")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
  }, 15000);

  it("still closes the add-evaluator form once that same evaluator's config is the one saved", async () => {
    const savedConfig: EvaluatorConfigOut = {
      evaluator_name: "toxicity",
      enabled: true,
      sampling_rate: 0.1,
      threshold: null,
      max_retries: 3,
      created_at: "2026-09-11T12:00:00Z",
      updated_at: "2026-09-11T12:00:00Z",
    };
    vi.mocked(fetchVigilProxy).mockResolvedValue(savedConfig);
    render(<EvaluatorConfigsPanel initialConfigs={[]} initialError={null} />);

    fireEvent.change(screen.getByLabelText(/evaluator name/i), { target: { value: "toxicity" } });
    fireEvent.click(screen.getByRole("button", { name: /configure/i }));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(screen.queryByRole("button", { name: /cancel/i })).not.toBeInTheDocument());
    // Back to the "add a new evaluator" affordance.
    expect(screen.getByLabelText(/evaluator name/i)).toHaveValue("");
  });

  it("offers a retry on initial load failure and recovers without a full page reload", async () => {
    render(
      <EvaluatorConfigsPanel
        initialConfigs={null}
        initialError={{ status: 503, message: "Telemetry storage is temporarily unavailable. Please retry." }}
      />,
    );

    expect(screen.getByText("Telemetry storage is temporarily unavailable. Please retry.")).toBeInTheDocument();
    const retryButton = screen.getByRole("button", { name: /retry/i });

    vi.mocked(fetchVigilProxy).mockResolvedValue({ configs: [RELEVANCE_CONFIG] });
    fireEvent.click(retryButton);

    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledWith("/api/vigil/evaluations/configs"));
    expect(await screen.findByText("relevance")).toBeInTheDocument();
    expect(
      screen.queryByText("Telemetry storage is temporarily unavailable. Please retry."),
    ).not.toBeInTheDocument();
  });

  it("shows the new failure message and keeps offering retry when a retry itself fails", async () => {
    render(
      <EvaluatorConfigsPanel
        initialConfigs={null}
        initialError={{ status: 503, message: "Telemetry storage is temporarily unavailable. Please retry." }}
      />,
    );

    vi.mocked(fetchVigilProxy).mockRejectedValue(new Error("network down"));
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    expect(await screen.findByText(/unable to load evaluator configurations/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
