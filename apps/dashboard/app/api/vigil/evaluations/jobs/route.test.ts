import { describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import type { EvaluationJobListResponse } from "@/lib/api/types";
import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/evaluations", () => ({
  listEvaluationJobs: vi.fn(),
}));

import { listEvaluationJobs } from "@/lib/api/evaluations";

import { GET } from "./route";

describe("GET /api/vigil/evaluations/jobs", () => {
  it("maps query params through to listEvaluationJobs", async () => {
    vi.mocked(listEvaluationJobs).mockResolvedValue({ jobs: [], next_cursor: null });

    await GET(
      new NextRequest(
        "http://localhost/api/vigil/evaluations/jobs?status=failed&evaluator_name=relevance&limit=10&cursor=abc",
      ),
    );

    expect(listEvaluationJobs).toHaveBeenCalledWith({
      status: "failed",
      evaluator_name: "relevance",
      limit: 10,
      cursor: "abc",
    });
  });

  it("omits absent params rather than sending null/empty values", async () => {
    vi.mocked(listEvaluationJobs).mockResolvedValue({ jobs: [], next_cursor: null });

    await GET(new NextRequest("http://localhost/api/vigil/evaluations/jobs"));

    expect(listEvaluationJobs).toHaveBeenCalledWith({
      status: undefined,
      evaluator_name: undefined,
      limit: undefined,
      cursor: undefined,
    });
  });

  it("returns the upstream response body on success", async () => {
    const body: EvaluationJobListResponse = {
      jobs: [
        {
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
        },
      ],
      next_cursor: null,
    };
    vi.mocked(listEvaluationJobs).mockResolvedValue(body);

    const response = await GET(new NextRequest("http://localhost/api/vigil/evaluations/jobs"));

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual(body);
  });

  it("preserves a 422 for a malformed cursor", async () => {
    vi.mocked(listEvaluationJobs).mockRejectedValue(new VigilApiError(422, "Malformed pagination cursor."));

    const response = await GET(new NextRequest("http://localhost/api/vigil/evaluations/jobs?cursor=garbage"));

    expect(response.status).toBe(422);
  });
});
