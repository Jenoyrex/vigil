import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchVigilProxy } from "./browserClient";
import { VigilApiError } from "./types";

function mockResponse(status: number, body: unknown, ok = status >= 200 && status < 300): Response {
  return { ok, status, json: async () => body } as Response;
}

describe("fetchVigilProxy", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns parsed JSON on a successful response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse(200, { ok: true })));
    const result = await fetchVigilProxy<{ ok: boolean }>("/api/vigil/traces");
    expect(result).toEqual({ ok: true });
  });

  it("builds the request URL from the given path and params", async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await fetchVigilProxy("/api/vigil/traces", { environment: "production" });
    expect(fetchMock).toHaveBeenCalledWith("/api/vigil/traces?environment=production", expect.any(Object));
  });

  it("calls only the given same-origin path -- never an absolute URL to the real API", async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await fetchVigilProxy("/api/vigil/analytics/spans");
    const [calledUrl] = fetchMock.mock.calls[0] as [string];
    expect(calledUrl.startsWith("/api/vigil/")).toBe(true);
  });

  it("throws VigilApiError carrying the upstream status and detail message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse(404, { detail: "Trace not found." }, false)));
    await expect(fetchVigilProxy("/api/vigil/traces/x")).rejects.toMatchObject({
      status: 404,
      message: "Trace not found.",
    });
  });

  it("falls back to a generic message when the error body has no detail field", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse(503, {}, false)));
    const error: unknown = await fetchVigilProxy("/api/vigil/traces").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(VigilApiError);
    expect((error as VigilApiError).status).toBe(503);
    expect((error as VigilApiError).message.length).toBeGreaterThan(0);
  });

  it("falls back to a generic message when the error body is not JSON at all", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => {
          throw new SyntaxError("Unexpected token");
        },
      } as unknown as Response),
    );
    await expect(fetchVigilProxy("/api/vigil/traces")).rejects.toBeInstanceOf(VigilApiError);
  });

  it("never sends an Authorization header -- the API key must never reach the browser", async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await fetchVigilProxy("/api/vigil/traces");
    const options = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    const headers = options?.headers as Record<string, string> | undefined;
    expect(headers?.["Authorization"]).toBeUndefined();
    expect(headers?.["authorization"]).toBeUndefined();
  });
});
