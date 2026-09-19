import { describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import type { EvaluatorConfigOut } from "@/lib/api/types";
import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/evaluations", () => ({
  getEvaluatorConfig: vi.fn(),
  upsertEvaluatorConfig: vi.fn(),
}));

import { getEvaluatorConfig, upsertEvaluatorConfig } from "@/lib/api/evaluations";

import { GET, PUT } from "./route";

const CONFIG: EvaluatorConfigOut = {
  evaluator_name: "relevance",
  enabled: true,
  sampling_rate: 0.5,
  threshold: 0.42,
  max_retries: 3,
  created_at: "2026-09-11T12:00:00Z",
  updated_at: "2026-09-11T12:00:00Z",
};

describe("GET /api/vigil/evaluations/configs/[evaluatorName]", () => {
  it("returns the upstream config on success", async () => {
    vi.mocked(getEvaluatorConfig).mockResolvedValue(CONFIG);

    const response = await GET(new NextRequest("http://localhost/api/vigil/evaluations/configs/relevance"), {
      params: Promise.resolve({ evaluatorName: "relevance" }),
    });

    expect(getEvaluatorConfig).toHaveBeenCalledWith("relevance");
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual(CONFIG);
  });

  it("preserves a 404 for an unconfigured evaluator", async () => {
    vi.mocked(getEvaluatorConfig).mockRejectedValue(
      new VigilApiError(404, "This evaluator has never been configured for this project."),
    );

    const response = await GET(new NextRequest("http://localhost/api/vigil/evaluations/configs/unknown"), {
      params: Promise.resolve({ evaluatorName: "unknown" }),
    });

    expect(response.status).toBe(404);
  });
});

describe("PUT /api/vigil/evaluations/configs/[evaluatorName]", () => {
  it("forwards the request body and returns the upstream response", async () => {
    vi.mocked(upsertEvaluatorConfig).mockResolvedValue(CONFIG);
    const body = { enabled: true, sampling_rate: 0.5, threshold: 0.42, max_retries: 3 };

    const response = await PUT(
      new NextRequest("http://localhost/api/vigil/evaluations/configs/relevance", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
      { params: Promise.resolve({ evaluatorName: "relevance" }) },
    );

    expect(upsertEvaluatorConfig).toHaveBeenCalledWith("relevance", body);
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual(CONFIG);
  });

  it("preserves a 422 validation error from the upstream API", async () => {
    vi.mocked(upsertEvaluatorConfig).mockRejectedValue(
      new VigilApiError(422, "sampling_rate must be between 0 and 1."),
    );

    const response = await PUT(
      new NextRequest("http://localhost/api/vigil/evaluations/configs/relevance", {
        method: "PUT",
        body: JSON.stringify({ enabled: true, sampling_rate: 5 }),
      }),
      { params: Promise.resolve({ evaluatorName: "relevance" }) },
    );

    expect(response.status).toBe(422);
    await expect(response.json()).resolves.toEqual({ detail: "sampling_rate must be between 0 and 1." });
  });

  it("maps an unparseable request body to a safe generic error, never a raw stack trace", async () => {
    const response = await PUT(
      new NextRequest("http://localhost/api/vigil/evaluations/configs/relevance", {
        method: "PUT",
        body: "not json",
        headers: { "Content-Type": "application/json" },
      }),
      { params: Promise.resolve({ evaluatorName: "relevance" }) },
    );

    expect(response.status).toBe(500);
    const responseBody = (await response.json()) as { detail: string };
    expect(responseBody.detail).toBe("An unexpected error occurred.");
  });
});
