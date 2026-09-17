import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/traces", () => ({
  getTrace: vi.fn(),
}));

import { getTrace } from "@/lib/api/traces";

import { proxy } from "./proxy";

// `proxy()` reads `process.env.NODE_ENV` per invocation (unlike
// next.config.ts's headers(), it isn't cached at module-evaluation time),
// so plain vi.stubEnv before each call is enough -- no module reset needed.
describe("proxy()", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.mocked(getTrace).mockReset();
  });

  describe("CSP nonce (production only)", () => {
    it("omits Content-Security-Policy entirely outside production", async () => {
      vi.stubEnv("NODE_ENV", "development");
      const response = await proxy(new NextRequest("http://localhost/"));
      expect(response.headers.get("Content-Security-Policy")).toBeNull();
      expect(response.headers.get("x-middleware-request-x-nonce")).toBeNull();
    });

    it("sets a Content-Security-Policy with a nonce'd script-src in production", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const response = await proxy(new NextRequest("http://localhost/"));
      const csp = response.headers.get("Content-Security-Policy") ?? "";
      const scriptSrc = csp.split(";").find((part) => part.trim().startsWith("script-src"));
      expect(scriptSrc).toMatch(/^\s*script-src 'self' 'nonce-[A-Za-z0-9+/=]+'$/);
    });

    it("never adds 'unsafe-inline' to script-src", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const response = await proxy(new NextRequest("http://localhost/"));
      const csp = response.headers.get("Content-Security-Policy") ?? "";
      const scriptSrc = csp.split(";").find((part) => part.trim().startsWith("script-src")) ?? "";
      expect(scriptSrc).not.toContain("unsafe-inline");
    });

    it("preserves every other directive unchanged", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const response = await proxy(new NextRequest("http://localhost/"));
      const csp = response.headers.get("Content-Security-Policy") ?? "";
      expect(csp).toContain("default-src 'self'");
      expect(csp).toContain("style-src 'self' 'unsafe-inline'");
      expect(csp).toContain("img-src 'self'");
      expect(csp).toContain("font-src 'self'");
      expect(csp).toContain("connect-src 'self'");
      expect(csp).toContain("frame-ancestors 'none'");
      expect(csp).toContain("base-uri 'self'");
      expect(csp).toContain("form-action 'self'");
      expect(csp).toContain("object-src 'none'");
    });

    it("generates a fresh, different nonce on every invocation", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const first = await proxy(new NextRequest("http://localhost/"));
      const second = await proxy(new NextRequest("http://localhost/"));
      const extractNonce = (csp: string) => /'nonce-([A-Za-z0-9+/=]+)'/.exec(csp)?.[1];
      const firstNonce = extractNonce(first.headers.get("Content-Security-Policy") ?? "");
      const secondNonce = extractNonce(second.headers.get("Content-Security-Policy") ?? "");
      expect(firstNonce).toBeTruthy();
      expect(secondNonce).toBeTruthy();
      expect(firstNonce).not.toBe(secondNonce);
    });

    it("forwards the same nonce to both the request Next.js renders with and the response", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const response = await proxy(new NextRequest("http://localhost/"));
      // NextResponse.next({ request: { headers } }) surfaces the forwarded
      // request headers on the returned response as x-middleware-request-*
      // (see next/dist/server/web/spec-extension/response.js) -- this is
      // how Next.js's own server later reconstructs the modified request.
      const forwardedNonceHeader = response.headers.get("x-middleware-request-x-nonce");
      const forwardedCsp = response.headers.get("x-middleware-request-content-security-policy") ?? "";
      const responseCsp = response.headers.get("Content-Security-Policy") ?? "";
      expect(forwardedNonceHeader).toBeTruthy();
      expect(forwardedCsp).toContain(`'nonce-${forwardedNonceHeader}'`);
      expect(responseCsp).toContain(`'nonce-${forwardedNonceHeader}'`);
    });
  });

  describe("trace-detail 404 rewrite (preserved from before the CSP change)", () => {
    it("rewrites to /trace-not-found on a confirmed-missing trace", async () => {
      vi.stubEnv("NODE_ENV", "production");
      vi.mocked(getTrace).mockRejectedValue(new VigilApiError(404, "Trace not found."));

      const response = await proxy(new NextRequest("http://localhost/traces/abc123"));

      expect(response.headers.get("x-middleware-rewrite")).toBe("http://localhost/trace-not-found");
    });

    it("falls through unchanged on a found trace", async () => {
      vi.stubEnv("NODE_ENV", "production");
      vi.mocked(getTrace).mockResolvedValue({} as never);

      const response = await proxy(new NextRequest("http://localhost/traces/abc123"));

      expect(response.headers.get("x-middleware-rewrite")).toBeNull();
    });

    it("falls through unchanged on a non-404 upstream error", async () => {
      vi.stubEnv("NODE_ENV", "production");
      vi.mocked(getTrace).mockRejectedValue(new VigilApiError(503, "Unable to reach the telemetry API."));

      const response = await proxy(new NextRequest("http://localhost/traces/abc123"));

      expect(response.headers.get("x-middleware-rewrite")).toBeNull();
    });

    it("still carries the CSP nonce on a rewritten response", async () => {
      vi.stubEnv("NODE_ENV", "production");
      vi.mocked(getTrace).mockRejectedValue(new VigilApiError(404, "Trace not found."));

      const response = await proxy(new NextRequest("http://localhost/traces/abc123"));

      expect(response.headers.get("Content-Security-Policy")).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
    });

    it("does not touch getTrace for non-trace-detail routes", async () => {
      vi.stubEnv("NODE_ENV", "production");
      await proxy(new NextRequest("http://localhost/evaluations"));
      expect(getTrace).not.toHaveBeenCalled();
    });
  });
});
