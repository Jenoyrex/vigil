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

  describe("PUT with a body (e.g. saving an evaluator config)", () => {
    it("sends the given method and JSON-serialized body", async () => {
      const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, { enabled: true }));
      vi.stubGlobal("fetch", fetchMock);

      await fetchVigilProxy("/api/vigil/evaluations/configs/relevance", undefined, {
        method: "PUT",
        body: { enabled: true, sampling_rate: 0.5 },
      });

      const options = fetchMock.mock.calls[0][1] as RequestInit;
      expect(options.method).toBe("PUT");
      expect(options.body).toBe(JSON.stringify({ enabled: true, sampling_rate: 0.5 }));
    });

    it("sets Content-Type: application/json only when a body is present", async () => {
      const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
      vi.stubGlobal("fetch", fetchMock);

      await fetchVigilProxy("/api/vigil/evaluations/configs/relevance", undefined, {
        method: "PUT",
        body: { enabled: false },
      });

      const options = fetchMock.mock.calls[0][1] as RequestInit;
      const headers = options.headers as Record<string, string>;
      expect(headers["Content-Type"]).toBe("application/json");
    });

    it("still returns the parsed JSON response on success", async () => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse(200, { enabled: true, sampling_rate: 0.5 })));

      const result = await fetchVigilProxy("/api/vigil/evaluations/configs/relevance", undefined, {
        method: "PUT",
        body: { enabled: true, sampling_rate: 0.5 },
      });

      expect(result).toEqual({ enabled: true, sampling_rate: 0.5 });
    });

    it("throws VigilApiError with the upstream detail on a validation failure", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(mockResponse(422, { detail: "sampling_rate must be between 0 and 1." }, false)),
      );

      await expect(
        fetchVigilProxy("/api/vigil/evaluations/configs/relevance", undefined, {
          method: "PUT",
          body: { enabled: true, sampling_rate: 5 },
        }),
      ).rejects.toMatchObject({ status: 422, message: "sampling_rate must be between 0 and 1." });
    });
  });

  it("existing GET call sites are unaffected: no body, no Content-Type, method left to fetch's own default", async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await fetchVigilProxy("/api/vigil/traces");
    const options = fetchMock.mock.calls[0][1] as RequestInit;
    expect(options.method).toBeUndefined();
    expect(options.body).toBeUndefined();
    expect((options.headers as Record<string, string> | undefined)?.["Content-Type"]).toBeUndefined();
  });
});
