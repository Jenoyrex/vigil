import { describe, expect, it, vi } from "vitest";

import type { EvaluatorConfigListResponse } from "@/lib/api/types";
import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/evaluations", () => ({
  listEvaluatorConfigs: vi.fn(),
}));

import { listEvaluatorConfigs } from "@/lib/api/evaluations";

import { GET } from "./route";

describe("GET /api/vigil/evaluations/configs", () => {
  it("returns the upstream response body on success", async () => {
    const body: EvaluatorConfigListResponse = {
      configs: [
        {
          evaluator_name: "relevance",
          enabled: true,
          sampling_rate: 0.1,
          threshold: null,
          max_retries: 3,
          created_at: "2026-09-11T12:00:00Z",
          updated_at: "2026-09-11T12:00:00Z",
        },
      ],
    };
    vi.mocked(listEvaluatorConfigs).mockResolvedValue(body);

    const response = await GET();

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual(body);
  });

  it("preserves the upstream status/detail on failure", async () => {
    vi.mocked(listEvaluatorConfigs).mockRejectedValue(new VigilApiError(401, "Invalid or missing API key."));

    const response = await GET();

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({ detail: "Invalid or missing API key." });
  });
});
