import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EvaluationResultOut, SpanEvaluationsResponse } from "@/lib/api/types";
import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/browserClient", () => ({
  fetchVigilProxy: vi.fn(),
}));

import { fetchVigilProxy } from "@/lib/api/browserClient";

import { SpanEvaluationsPanel } from "./SpanEvaluationsPanel";

// See EvaluatorConfigForm.test.tsx's identical comment: this mocked module
// is shared across every test in this file.
beforeEach(() => {
  vi.mocked(fetchVigilProxy).mockReset();
});

const TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736";
const SPAN_ID = "00f067aa0ba902b7";

const RESULT: EvaluationResultOut = {
  evaluation_id: "22222222-2222-2222-2222-222222222222",
  trace_id: TRACE_ID,
  span_id: SPAN_ID,
  evaluator_name: "relevance",
  evaluator_version: "0.1.0",
  score: 0.87,
  label: "relevant",
  explanation: "cosine similarity above threshold",
  evaluator_model: "tfidf",
  evaluator_provider: null,
  evaluation_latency_ms: 3.35,
  evaluation_cost_usd: "0.000340",
  job_created_at: "2026-09-11T12:00:00Z",
  written_at: "2026-09-11T12:00:01Z",
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

describe("SpanEvaluationsPanel", () => {
  it("fetches evaluations for exactly the given trace/span, once", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({ results: [] } satisfies SpanEvaluationsResponse);
    render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);

    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));
    expect(fetchVigilProxy).toHaveBeenCalledWith(
      `/api/vigil/traces/${TRACE_ID}/spans/${SPAN_ID}/evaluations`,
    );
  });

  it("shows a loading placeholder while the request is in flight", () => {
    vi.mocked(fetchVigilProxy).mockReturnValue(deferred<SpanEvaluationsResponse>().promise);
    render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);
    expect(screen.getByText("Evaluations")).toBeInTheDocument();
    expect(screen.queryByText(/no evaluations/i)).not.toBeInTheDocument();
  });

  it("shows an empty-state message when the span has no evaluations", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({ results: [] });
    render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);
    expect(await screen.findByText(/no evaluations for this span yet/i)).toBeInTheDocument();
  });

  it("renders score, label, explanation, model/provider, and cost for a populated result", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({ results: [RESULT] });
    render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);

    expect(await screen.findByText("relevant")).toBeInTheDocument();
    expect(screen.getByText("cosine similarity above threshold")).toBeInTheDocument();
    expect(screen.getByText("0.87")).toBeInTheDocument();
    expect(screen.getByText("tfidf")).toBeInTheDocument();
    // formatCost renders the Decimal string as currency, never as a parsed float.
    expect(screen.getByText("$0.00034")).toBeInTheDocument();
  });

  it("omits the Model/Provider rows when the API returns them as null", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({
      results: [{ ...RESULT, evaluator_model: null, evaluator_provider: null }],
    });
    render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);
    await screen.findByText("relevant");
    expect(screen.queryByText("Model")).not.toBeInTheDocument();
    expect(screen.queryByText("Provider")).not.toBeInTheDocument();
  });

  it("renders multiple results, one per evaluator that has run", async () => {
    const second: EvaluationResultOut = { ...RESULT, evaluation_id: "33333333-3333-3333-3333-333333333333", evaluator_name: "relevance_embedding", label: "irrelevant" };
    vi.mocked(fetchVigilProxy).mockResolvedValue({ results: [RESULT, second] });
    render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);

    expect(await screen.findByText("relevant")).toBeInTheDocument();
    expect(screen.getByText("irrelevant")).toBeInTheDocument();
  });

  it("shows a safe error message when the request fails", async () => {
    vi.mocked(fetchVigilProxy).mockRejectedValue(
      new VigilApiError(503, "Telemetry storage is temporarily unavailable. Please retry."),
    );
    render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);
    expect(await screen.findByText("Telemetry storage is temporarily unavailable. Please retry.")).toBeInTheDocument();
  });

  it("re-fetches when the selected span changes", async () => {
    vi.mocked(fetchVigilProxy).mockResolvedValue({ results: [] });
    const { rerender } = render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);
    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(1));

    rerender(<SpanEvaluationsPanel traceId={TRACE_ID} spanId="ffffffffffffffff" />);
    await waitFor(() => expect(fetchVigilProxy).toHaveBeenCalledTimes(2));
    expect(fetchVigilProxy).toHaveBeenLastCalledWith(
      `/api/vigil/traces/${TRACE_ID}/spans/ffffffffffffffff/evaluations`,
    );
  });

  it("does not let a stale, slower response overwrite a newer selection's result", async () => {
    const first = deferred<SpanEvaluationsResponse>();
    const second = deferred<SpanEvaluationsResponse>();
    vi.mocked(fetchVigilProxy).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    const { rerender } = render(<SpanEvaluationsPanel traceId={TRACE_ID} spanId={SPAN_ID} />);
    rerender(<SpanEvaluationsPanel traceId={TRACE_ID} spanId="ffffffffffffffff" />);

    // The newer (second) request resolves first...
    second.resolve({ results: [{ ...RESULT, label: "from-second-span" }] });
    await screen.findByText("from-second-span");

    // ...then the older (first, now-stale) request resolves late. It must
    // never overwrite what the newer selection already rendered.
    first.resolve({ results: [{ ...RESULT, label: "from-first-span-stale" }] });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("from-first-span-stale")).not.toBeInTheDocument();
    expect(screen.getByText("from-second-span")).toBeInTheDocument();
  });
});
