import { describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import type { SpanEvaluationsResponse } from "@/lib/api/types";
import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/evaluations", () => ({
  getSpanEvaluations: vi.fn(),
}));

import { getSpanEvaluations } from "@/lib/api/evaluations";

import { GET } from "./route";

const TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736";
const SPAN_ID = "00f067aa0ba902b7";

describe("GET /api/vigil/traces/[traceId]/spans/[spanId]/evaluations", () => {
  it("calls getSpanEvaluations with the path params", async () => {
    vi.mocked(getSpanEvaluations).mockResolvedValue({ results: [] });

    await GET(new NextRequest(`http://localhost/api/vigil/traces/${TRACE_ID}/spans/${SPAN_ID}/evaluations`), {
      params: Promise.resolve({ traceId: TRACE_ID, spanId: SPAN_ID }),
    });

    expect(getSpanEvaluations).toHaveBeenCalledWith(TRACE_ID, SPAN_ID);
  });

  it("returns an empty results list as a normal 200, not an error", async () => {
    vi.mocked(getSpanEvaluations).mockResolvedValue({ results: [] });

    const response = await GET(new NextRequest(`http://localhost/api/vigil/traces/${TRACE_ID}/spans/${SPAN_ID}/evaluations`), {
      params: Promise.resolve({ traceId: TRACE_ID, spanId: SPAN_ID }),
    });

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ results: [] });
  });

  it("returns populated results on success", async () => {
    const body: SpanEvaluationsResponse = {
      results: [
        {
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
          evaluation_cost_usd: null,
          job_created_at: "2026-09-11T12:00:00Z",
          written_at: "2026-09-11T12:00:01Z",
        },
      ],
    };
    vi.mocked(getSpanEvaluations).mockResolvedValue(body);

    const response = await GET(new NextRequest(`http://localhost/api/vigil/traces/${TRACE_ID}/spans/${SPAN_ID}/evaluations`), {
      params: Promise.resolve({ traceId: TRACE_ID, spanId: SPAN_ID }),
    });

    await expect(response.json()).resolves.toEqual(body);
  });

  it("preserves a 503 when ClickHouse is unavailable upstream", async () => {
    vi.mocked(getSpanEvaluations).mockRejectedValue(
      new VigilApiError(503, "Telemetry storage is temporarily unavailable. Please retry."),
    );

    const response = await GET(new NextRequest(`http://localhost/api/vigil/traces/${TRACE_ID}/spans/${SPAN_ID}/evaluations`), {
      params: Promise.resolve({ traceId: TRACE_ID, spanId: SPAN_ID }),
    });

    expect(response.status).toBe(503);
  });
});
