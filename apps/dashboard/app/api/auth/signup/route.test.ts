import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { VigilApiError } from "@/lib/api/types";

vi.mock("@/lib/api/dashboardAuth", () => ({
  signup: vi.fn(),
  SESSION_COOKIE_NAME: "vigil_dashboard_session",
  PROJECT_COOKIE_NAME: "vigil_project",
  sessionCookieOptions: (expiresAt: string) => ({
    httpOnly: true,
    secure: false,
    sameSite: "strict" as const,
    path: "/",
    expires: new Date(expiresAt),
  }),
}));

import { signup } from "@/lib/api/dashboardAuth";

import { POST } from "./route";

function request(body: unknown, contentType = "application/json"): NextRequest {
  return new NextRequest("http://localhost/api/auth/signup", {
    method: "POST",
    headers: { "content-type": contentType },
    body: JSON.stringify(body),
  });
}

/** A delete is a Set-Cookie with an empty value that expires immediately. */
function clearsProjectCookie(response: Response): boolean {
  const header = response.headers.getSetCookie().find((c) => c.startsWith("vigil_project="));
  return header !== undefined && /^vigil_project=;/.test(header) && /Expires=Thu, 01 Jan 1970|Max-Age=0/i.test(header);
}

describe("POST /api/auth/signup", () => {
  afterEach(() => vi.mocked(signup).mockReset());

  it("sets the HttpOnly session cookie and never echoes the token", async () => {
    vi.mocked(signup).mockResolvedValue({ sessionToken: "raw-token", expiresAt: "2099-01-01T00:00:00Z" });
    const response = await POST(
      request({ email: "new@example.com", password: "long enough password", fullName: " Ada " }),
    );
    expect(response.status).toBe(201);
    expect(JSON.stringify(await response.json())).not.toContain("raw-token");
    const cookie = response.cookies.get("vigil_dashboard_session");
    expect(cookie?.value).toBe("raw-token");
    expect(cookie?.httpOnly).toBe(true);
    expect(vi.mocked(signup).mock.calls[0][0]).toEqual({
      email: "new@example.com",
      password: "long enough password",
      full_name: "Ada",
    });
  });

  it("clears a previous user's project cookie on success, keeping the new session cookie", async () => {
    vi.mocked(signup).mockResolvedValue({ sessionToken: "raw-token", expiresAt: "2099-01-01T00:00:00Z" });
    const req = request({ email: "new@example.com", password: "long enough password" });
    req.cookies.set("vigil_project", "someone-elses-project");

    const response = await POST(req);

    expect(response.status).toBe(201);
    expect(clearsProjectCookie(response)).toBe(true);
    expect(response.cookies.get("vigil_dashboard_session")?.value).toBe("raw-token");
  });

  it("rejects non-JSON bodies before calling the API (login-CSRF guard)", async () => {
    const response = await POST(request({ email: "a@b.co", password: "x" }, "text/plain"));
    expect(response.status).toBe(400);
    expect(signup).not.toHaveBeenCalled();
  });

  it("passes the API's duplicate-email error through", async () => {
    vi.mocked(signup).mockRejectedValue(new VigilApiError(409, "An account with this email already exists."));
    const response = await POST(request({ email: "dup@example.com", password: "long enough password" }));
    expect(response.status).toBe(409);
    expect((await response.json()).detail).toMatch(/already exists/);
    expect(response.cookies.get("vigil_dashboard_session")).toBeUndefined();
    expect(response.headers.getSetCookie().some((c) => c.startsWith("vigil_project="))).toBe(false);
  });
});
