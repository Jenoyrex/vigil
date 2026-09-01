import { describe, expect, it } from "vitest";

import { VigilApiError } from "@/lib/api/types";

import { handleVigilError } from "./handleError";

describe("handleVigilError", () => {
  it("preserves the upstream status code and message for a VigilApiError", async () => {
    const response = handleVigilError(new VigilApiError(404, "Trace not found."));
    expect(response.status).toBe(404);
    await expect(response.json()).resolves.toEqual({ detail: "Trace not found." });
  });

  it("preserves a 422 validation error", async () => {
    const response = handleVigilError(new VigilApiError(422, "Time window must not exceed 7 days."));
    expect(response.status).toBe(422);
    await expect(response.json()).resolves.toEqual({ detail: "Time window must not exceed 7 days." });
  });

  it("maps an unexpected (non-VigilApiError) failure to a generic 500 -- never leaking internals", async () => {
    const response = handleVigilError(new Error("ECONNREFUSED 10.0.0.5:8123 password=hunter2"));
    expect(response.status).toBe(500);
    const body = (await response.json()) as { detail: string };
    expect(body.detail).toBe("An unexpected error occurred.");
    expect(body.detail).not.toContain("ECONNREFUSED");
    expect(body.detail).not.toContain("hunter2");
  });
});
