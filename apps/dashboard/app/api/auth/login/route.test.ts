import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/dashboardAuth", () => ({
  login: vi.fn(),
  SESSION_COOKIE_NAME: "vigil_dashboard_session",
  sessionCookieOptions: (expiresAt: string) => ({
    httpOnly: true,
    secure: false,
    sameSite: "strict" as const,
    path: "/",
    expires: new Date(expiresAt),
  }),
}));

import { login } from "@/lib/api/dashboardAuth";

import { POST } from "./route";

function jsonRequest(body: unknown, contentType = "application/json"): NextRequest {
  return new NextRequest("http://localhost/api/auth/login", {
    method: "POST",
    headers: { "content-type": contentType },
    body: JSON.stringify(body),
  });
}

describe("POST /api/auth/login", () => {
  afterEach(() => {
    vi.mocked(login).mockReset();
    vi.unstubAllEnvs();
  });

  it("sets an HttpOnly session cookie on success and never echoes the token in the body", async () => {
    vi.mocked(login).mockResolvedValue({
      sessionToken: "raw-token-value",
      expiresAt: "2099-01-01T00:00:00Z",
    });

    const response = await POST(jsonRequest({ email: "owner@example.com", password: "hunter2hunter2" }));

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ ok: true });
    expect(JSON.stringify(body)).not.toContain("raw-token-value");

    const cookie = response.cookies.get("vigil_dashboard_session");
    expect(cookie?.value).toBe("raw-token-value");
  });

  it("passes no client IP by default (no trusted proxy), even when the browser sent X-Forwarded-For", async () => {
    vi.mocked(login).mockResolvedValue({ sessionToken: "t", expiresAt: "2099-01-01T00:00:00Z" });
    const request = new NextRequest("http://localhost/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json", "x-forwarded-for": "6.6.6.6" },
      body: JSON.stringify({ email: "owner@example.com", password: "pw" }),
    });

    await POST(request);

    expect(login).toHaveBeenCalledWith("owner@example.com", "pw", null);
  });

  it("passes the trusted-proxy-derived client IP, ignoring spoofed left-hand entries", async () => {
    vi.stubEnv("VIGIL_DASHBOARD_TRUSTED_PROXY_HOPS", "1");
    vi.mocked(login).mockResolvedValue({ sessionToken: "t", expiresAt: "2099-01-01T00:00:00Z" });
    const request = new NextRequest("http://localhost/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json", "x-forwarded-for": "6.6.6.6, 203.0.113.7" },
      body: JSON.stringify({ email: "owner@example.com", password: "pw" }),
    });

    await POST(request);

    expect(login).toHaveBeenCalledWith("owner@example.com", "pw", "203.0.113.7");
  });

  it("preserves the upstream status/detail on a failed login", async () => {
    vi.mocked(login).mockRejectedValue(new VigilApiError(401, "Invalid email or password."));

    const response = await POST(jsonRequest({ email: "owner@example.com", password: "wrong" }));

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({ detail: "Invalid email or password." });
  });

  it("rejects a request missing email/password with a 400, never calling login()", async () => {
    const response = await POST(jsonRequest({ email: "owner@example.com" }));

    expect(response.status).toBe(400);
    expect(login).not.toHaveBeenCalled();
  });

  it("rejects a malformed (non-JSON) body with a 400", async () => {
    const request = new NextRequest("http://localhost/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "not json",
    });

    const response = await POST(request);

    expect(response.status).toBe(400);
    expect(login).not.toHaveBeenCalled();
  });

  it("rejects a non-application/json content type before ever parsing the body -- closes the text/plain login-CSRF bypass", async () => {
    const request = jsonRequest(
      { email: "victim@example.com", password: "whatever" },
      "text/plain;charset=UTF-8",
    );

    const response = await POST(request);

    expect(response.status).toBe(400);
    expect(login).not.toHaveBeenCalled();
  });
});
