import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { login } from "./dashboardAuth";

const LOGIN_OK = { session_token: "tok", expires_at: "2099-01-01T00:00:00Z" };

function stubFetch() {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(LOGIN_OK), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sentHeaders(fetchMock: ReturnType<typeof stubFetch>): Record<string, string> {
  return fetchMock.mock.calls[0][1].headers as Record<string, string>;
}

describe("login() client-IP attribution", () => {
  beforeEach(() => {
    vi.stubEnv("VIGIL_API_BASE_URL", "http://api:8000");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("sends the derived client IP together with the trusted credential", async () => {
    vi.stubEnv("VIGIL_API_DASHBOARD_CLIENT_IP_TOKEN", "shared-secret");
    const fetchMock = stubFetch();

    await login("owner@example.com", "pw", "203.0.113.7");

    const headers = sentHeaders(fetchMock);
    expect(headers["X-Vigil-Client-IP"]).toBe("203.0.113.7");
    expect(headers["X-Vigil-Dashboard-Token"]).toBe("shared-secret");
  });

  it("sends neither header when no client IP could be derived", async () => {
    vi.stubEnv("VIGIL_API_DASHBOARD_CLIENT_IP_TOKEN", "shared-secret");
    const fetchMock = stubFetch();

    await login("owner@example.com", "pw", null);

    const headers = sentHeaders(fetchMock);
    expect(headers["X-Vigil-Client-IP"]).toBeUndefined();
    expect(headers["X-Vigil-Dashboard-Token"]).toBeUndefined();
  });

  it("sends neither header when the credential is not configured", async () => {
    vi.stubEnv("VIGIL_API_DASHBOARD_CLIENT_IP_TOKEN", "");
    const fetchMock = stubFetch();

    await login("owner@example.com", "pw", "203.0.113.7");

    const headers = sentHeaders(fetchMock);
    expect(headers["X-Vigil-Client-IP"]).toBeUndefined();
    expect(headers["X-Vigil-Dashboard-Token"]).toBeUndefined();
  });

  it("never forwards browser forwarding headers -- only Content-Type plus the two derived ones", async () => {
    vi.stubEnv("VIGIL_API_DASHBOARD_CLIENT_IP_TOKEN", "shared-secret");
    const fetchMock = stubFetch();

    await login("owner@example.com", "pw", "203.0.113.7");

    expect(Object.keys(sentHeaders(fetchMock)).sort()).toEqual([
      "Content-Type",
      "X-Vigil-Client-IP",
      "X-Vigil-Dashboard-Token",
    ]);
  });
});
