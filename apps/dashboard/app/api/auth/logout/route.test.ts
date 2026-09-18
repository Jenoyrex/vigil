import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

vi.mock("@/lib/api/dashboardAuth", () => ({
  logout: vi.fn(),
  SESSION_COOKIE_NAME: "vigil_dashboard_session",
}));

import { logout } from "@/lib/api/dashboardAuth";

import { POST } from "./route";

function requestWithCookie(cookie?: string): NextRequest {
  return new NextRequest("http://localhost/api/auth/logout", {
    method: "POST",
    headers: cookie ? { cookie } : undefined,
  });
}

describe("POST /api/auth/logout", () => {
  afterEach(() => {
    vi.mocked(logout).mockReset();
  });

  it("revokes the session and clears the cookie when one is present", async () => {
    const response = await POST(requestWithCookie("vigil_dashboard_session=a-real-token"));

    expect(response.status).toBe(204);
    expect(logout).toHaveBeenCalledWith("a-real-token");
    const cookie = response.cookies.get("vigil_dashboard_session");
    expect(cookie?.value).toBe("");
  });

  it("is idempotent -- no cookie present never calls logout() and still returns 204", async () => {
    const response = await POST(requestWithCookie());

    expect(response.status).toBe(204);
    expect(logout).not.toHaveBeenCalled();
  });
});
