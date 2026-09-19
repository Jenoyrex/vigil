import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/traces", () => ({
  getTrace: vi.fn(),
}));

vi.mock("@/lib/api/dashboardAuth", () => ({
  SESSION_COOKIE_NAME: "vigil_dashboard_session",
  validateSession: vi.fn(),
}));

import { getTrace } from "@/lib/api/traces";
import { validateSession } from "@/lib/api/dashboardAuth";

import { proxy } from "./proxy";

const VALID_SESSION = { userId: "u1", email: "owner@example.com", expiresAt: "2099-01-01T00:00:00Z" };

/** A request carrying a session cookie `validateSession` (mocked above) treats as valid by default. */
function authedRequest(url: string): NextRequest {
  return new NextRequest(url, {
    headers: { cookie: "vigil_dashboard_session=a-valid-token" },
  });
}

// `proxy()` reads `process.env.NODE_ENV` per invocation (unlike
// next.config.ts's headers(), it isn't cached at module-evaluation time),
// so plain vi.stubEnv before each call is enough -- no module reset needed.
//
// Every pre-existing test in this file predates the Phase 4D F1 session
// gate and exercises CSP/trace-rewrite behavior that only runs *after* that
// gate passes -- each one now sends a cookie via `authedRequest` and mocks
// `validateSession` to resolve it as valid, so the gate itself is covered
// separately, below, in its own describe block.
describe("proxy()", () => {
  beforeEach(() => {
    vi.mocked(validateSession).mockResolvedValue(VALID_SESSION);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.mocked(getTrace).mockReset();
    vi.mocked(validateSession).mockReset();
  });

  describe("CSP nonce (production only)", () => {
    it("omits Content-Security-Policy entirely outside production", async () => {
      vi.stubEnv("NODE_ENV", "development");
      const response = await proxy(authedRequest("http://localhost/"));
      expect(response.headers.get("Content-Security-Policy")).toBeNull();
      expect(response.headers.get("x-middleware-request-x-nonce")).toBeNull();
    });

    it("sets a Content-Security-Policy with a nonce'd script-src in production", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const response = await proxy(authedRequest("http://localhost/"));
      const csp = response.headers.get("Content-Security-Policy") ?? "";
      const scriptSrc = csp.split(";").find((part) => part.trim().startsWith("script-src"));
      expect(scriptSrc).toMatch(/^\s*script-src 'self' 'nonce-[A-Za-z0-9+/=]+'$/);
    });

    it("never adds 'unsafe-inline' to script-src", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const response = await proxy(authedRequest("http://localhost/"));
      const csp = response.headers.get("Content-Security-Policy") ?? "";
      const scriptSrc = csp.split(";").find((part) => part.trim().startsWith("script-src")) ?? "";
      expect(scriptSrc).not.toContain("unsafe-inline");
    });

    it("preserves every other directive unchanged", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const response = await proxy(authedRequest("http://localhost/"));
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
      const first = await proxy(authedRequest("http://localhost/"));
      const second = await proxy(authedRequest("http://localhost/"));
      const extractNonce = (csp: string) => /'nonce-([A-Za-z0-9+/=]+)'/.exec(csp)?.[1];
      const firstNonce = extractNonce(first.headers.get("Content-Security-Policy") ?? "");
      const secondNonce = extractNonce(second.headers.get("Content-Security-Policy") ?? "");
      expect(firstNonce).toBeTruthy();
      expect(secondNonce).toBeTruthy();
      expect(firstNonce).not.toBe(secondNonce);
    });

    it("forwards the same nonce to both the request Next.js renders with and the response", async () => {
      vi.stubEnv("NODE_ENV", "production");
      const response = await proxy(authedRequest("http://localhost/"));
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

      const response = await proxy(authedRequest("http://localhost/traces/abc123"));

      expect(response.headers.get("x-middleware-rewrite")).toBe("http://localhost/trace-not-found");
    });

    it("falls through unchanged on a found trace", async () => {
      vi.stubEnv("NODE_ENV", "production");
      vi.mocked(getTrace).mockResolvedValue({} as never);

      const response = await proxy(authedRequest("http://localhost/traces/abc123"));

      expect(response.headers.get("x-middleware-rewrite")).toBeNull();
    });

    it("falls through unchanged on a non-404 upstream error", async () => {
      vi.stubEnv("NODE_ENV", "production");
      vi.mocked(getTrace).mockRejectedValue(new VigilApiError(503, "Unable to reach the telemetry API."));

      const response = await proxy(authedRequest("http://localhost/traces/abc123"));

      expect(response.headers.get("x-middleware-rewrite")).toBeNull();
    });

    it("still carries the CSP nonce on a rewritten response", async () => {
      vi.stubEnv("NODE_ENV", "production");
      vi.mocked(getTrace).mockRejectedValue(new VigilApiError(404, "Trace not found."));

      const response = await proxy(authedRequest("http://localhost/traces/abc123"));

      expect(response.headers.get("Content-Security-Policy")).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
    });

    it("does not touch getTrace for non-trace-detail routes", async () => {
      vi.stubEnv("NODE_ENV", "production");
      await proxy(authedRequest("http://localhost/evaluations"));
      expect(getTrace).not.toHaveBeenCalled();
    });
  });

  describe("session gate (Phase 4D, F1)", () => {
    it("redirects an unauthenticated page request to /login, preserving the destination", async () => {
      const response = await proxy(new NextRequest("http://localhost/traces"));
      expect(response.status).toBe(307);
      const location = new URL(response.headers.get("location") ?? "");
      expect(location.pathname).toBe("/login");
      expect(location.searchParams.get("next")).toBe("/traces");
      expect(validateSession).not.toHaveBeenCalled();
    });

    it("preserves the full path AND query string of the protected destination in ?next=", async () => {
      const response = await proxy(
        new NextRequest("http://localhost/traces?status=failed&cursor=abc"),
      );
      expect(response.status).toBe(307);
      const location = new URL(response.headers.get("location") ?? "");
      expect(location.pathname).toBe("/login");
      expect(location.searchParams.get("next")).toBe("/traces?status=failed&cursor=abc");
    });

    it("returns a generic 401 JSON body for an unauthenticated /api/vigil/** request, never a redirect", async () => {
      const response = await proxy(new NextRequest("http://localhost/api/vigil/traces"));
      expect(response.status).toBe(401);
      expect(response.headers.get("location")).toBeNull();
      await expect(response.json()).resolves.toEqual({ detail: "Invalid or expired session." });
    });

    it("redirects when the cookie is present but the API rejects it (expired/revoked)", async () => {
      vi.mocked(validateSession).mockResolvedValue(null);
      const response = await proxy(authedRequest("http://localhost/"));
      expect(response.status).toBe(307);
      expect(new URL(response.headers.get("location") ?? "").pathname).toBe("/login");
    });

    it("passes through to CSP/trace logic once the cookie validates", async () => {
      const response = await proxy(authedRequest("http://localhost/"));
      expect(response.status).not.toBe(307);
      expect(validateSession).toHaveBeenCalledWith("a-valid-token");
    });

    it("never gates /login itself", async () => {
      const response = await proxy(new NextRequest("http://localhost/login"));
      expect(response.status).not.toBe(307);
      expect(validateSession).not.toHaveBeenCalled();
    });

    it("never gates /api/auth/login or /api/auth/logout", async () => {
      const loginRoute = await proxy(new NextRequest("http://localhost/api/auth/login", { method: "POST" }));
      const logoutRoute = await proxy(new NextRequest("http://localhost/api/auth/logout", { method: "POST" }));
      expect(loginRoute.status).not.toBe(401);
      expect(logoutRoute.status).not.toBe(401);
      expect(validateSession).not.toHaveBeenCalled();
    });

    it("cannot be bypassed by a spoofed next-router-prefetch header on a protected page", async () => {
      const response = await proxy(
        new NextRequest("http://localhost/traces", { headers: { "next-router-prefetch": "1" } }),
      );
      expect(response.status).toBe(307);
    });

    it("cannot be bypassed by a spoofed purpose: prefetch header on a protected /api/vigil/** route", async () => {
      const response = await proxy(
        new NextRequest("http://localhost/api/vigil/traces", { headers: { purpose: "prefetch" } }),
      );
      expect(response.status).toBe(401);
    });
  });
});
