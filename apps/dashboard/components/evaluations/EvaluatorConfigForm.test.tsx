import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EvaluatorConfigOut, EvaluatorConfigUpsertRequest } from "@/lib/api/types";
import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/browserClient", () => ({
  fetchVigilProxy: vi.fn(),
}));

import { fetchVigilProxy } from "@/lib/api/browserClient";

import { EvaluatorConfigForm } from "./EvaluatorConfigForm";

// The mocked module is evaluated once and shared across every test in this
// file -- reset call history/implementation before each test so an earlier
// test's call count/return value can never leak into a later assertion.
beforeEach(() => {
  vi.mocked(fetchVigilProxy).mockReset();
});

const EXISTING_CONFIG: EvaluatorConfigOut = {
  evaluator_name: "relevance",
  enabled: true,
  sampling_rate: 0.25,
  threshold: 0.42,
  max_retries: 5,
  created_at: "2026-09-11T12:00:00Z",
  updated_at: "2026-09-11T12:00:00Z",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("EvaluatorConfigForm", () => {
  it("pre-fills fields from an existing config", () => {
    render(
      <EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={vi.fn()} />,
    );
    expect(screen.getByLabelText(/sampling rate/i)).toHaveValue(0.25);
    expect(screen.getByLabelText(/threshold/i)).toHaveValue(0.42);
    expect(screen.getByLabelText(/max retries/i)).toHaveValue(5);
    expect(screen.getByRole("checkbox", { name: /enabled/i })).toBeChecked();
  });

  it("uses the API's documented defaults for a new (null) config", () => {
    render(
      <EvaluatorConfigForm evaluatorName="relevance_embedding" initialConfig={null} onSaved={vi.fn()} />,
    );
    expect(screen.getByLabelText(/sampling rate/i)).toHaveValue(0.1);
    expect(screen.getByLabelText(/threshold/i)).toHaveValue(null);
    expect(screen.getByLabelText(/max retries/i)).toHaveValue(3);
    expect(screen.getByRole("checkbox", { name: /enabled/i })).not.toBeChecked();
  });

  it("submits the complete configuration body on save -- full-replace PUT semantics", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue(EXISTING_CONFIG);
    render(
      <EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={vi.fn()} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));
    const [path, params, init] = vi.mocked(fetchVigilProxy).mock.calls[0];
    expect(path).toBe("/api/vigil/evaluations/configs/relevance");
    expect(params).toBeUndefined();
    expect(init).toEqual({
      method: "PUT",
      body: { enabled: true, sampling_rate: 0.25, threshold: 0.42, max_retries: 5 } satisfies EvaluatorConfigUpsertRequest,
    });
  });

  it("sends the documented defaults for any field left blank, never an omitted field", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue(EXISTING_CONFIG);
    // Start from a fully-populated existing config, then blank every
    // numeric field out by hand -- proves the component falls back to the
    // API's own documented defaults (0.1 / null / 3) on submit, rather than
    // sending an empty string, NaN, or silently reusing the prior value.
    render(
      <EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={vi.fn()} />,
    );

    fireEvent.change(screen.getByLabelText(/sampling rate/i), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText(/threshold/i), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText(/max retries/i), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));
    const [, , init] = vi.mocked(fetchVigilProxy).mock.calls[0];
    expect(init).toMatchObject({
      body: { enabled: true, sampling_rate: 0.1, threshold: null, max_retries: 3 },
    });
  });

  it("disables the Save button and ignores a second submit while a save is in flight", async () => {
    const pending = deferred<EvaluatorConfigOut>();
    vi.mocked(fetchVigilProxy).mockReturnValue(pending.promise);
    render(<EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={vi.fn()} />);

    const saveButton = screen.getByRole("button", { name: /save/i });
    fireEvent.click(saveButton);
    expect(saveButton).toBeDisabled();

    // A second submit while still saving must not issue a second request.
    fireEvent.click(saveButton);
    expect(fetchVigilProxy).toHaveBeenCalledTimes(1);

    pending.resolve(EXISTING_CONFIG);
    await waitFor(() => expect(saveButton).not.toBeDisabled());
  });

  it("shows a saved confirmation after a successful save", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue(EXISTING_CONFIG);
    render(<EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("calls onSaved with the response from the API", async () => {
    const onSaved = vi.fn();
    vi.mocked(fetchVigilProxy).mockResolvedValue(EXISTING_CONFIG);
    render(<EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={onSaved} />);

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(EXISTING_CONFIG));
  });

  it("shows the upstream error message and re-enables Save when the request fails", async () => {
    vi.mocked(fetchVigilProxy).mockRejectedValue(
      new VigilApiError(422, "sampling_rate must be between 0 and 1."),
    );
    render(<EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    expect(await screen.findByText("sampling_rate must be between 0 and 1.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save/i })).not.toBeDisabled();
  });

  it("clears a stale saved/error status as soon as the user edits a field again", async () => {
    vi.mocked(fetchVigilProxy).mockRejectedValue(new VigilApiError(500, "Something went wrong."));
    render(<EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText("Something went wrong.")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/max retries/i), { target: { value: "4" } });
    expect(screen.queryByText("Something went wrong.")).not.toBeInTheDocument();
  });

  it("calls onCancel when Cancel is clicked", () => {
    const onCancel = vi.fn();
    render(
      <EvaluatorConfigForm evaluatorName="relevance" initialConfig={null} onSaved={vi.fn()} onCancel={onCancel} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("omits the Cancel button when onCancel is not provided", () => {
    render(<EvaluatorConfigForm evaluatorName="relevance" initialConfig={EXISTING_CONFIG} onSaved={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /cancel/i })).not.toBeInTheDocument();
  });
});
