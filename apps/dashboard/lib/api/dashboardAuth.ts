import "server-only";

import { VigilApiError, extractDetailMessage, type ApiErrorBody } from "./types";

/**
 * Calls apps/api's dashboard-user authentication endpoints
 * (POST /v1/auth/login, POST /v1/auth/logout, GET /v1/auth/session).
 *
 * Deliberately a separate module from lib/api/vigilClient.ts: that module
 * holds the one server-side project API key (`VIGIL_API_KEY`) every
 * telemetry/analytics/evaluations call uses, completely independent of
 * which -- or whether any -- dashboard user is logged in. This module
 * never reads or sends that key, and vigilClient.ts never reads or sends
 * a session token. Presenting a session token to a vigilClient.ts-proxied
 * route, or the project API key to one of these functions, authenticates
 * nothing on either side -- see apps/api/app/api/v1/auth.py's module
 * docstring for the same separation on the server.
 *
 * Callers: proxy.ts (session validation gate, on every protected
 * request), app/api/auth/login/route.ts, app/api/auth/logout/route.ts.
 * `import "server-only"` above makes the build fail if this module is
 * ever imported, even transitively, by a Client Component -- the same
 * guard vigilClient.ts uses, for the same reason: nothing here should
 * ever reach browser-bundled code.
 */

const SESSION_TOKEN_HEADER = "X-Vigil-Session-Token";

/**
 * The dashboard's own cookie name for the session token -- distinct from
 * `SESSION_TOKEN_HEADER` above, which is the header this app's *server*
 * uses to present that token to apps/api. The browser only ever sees this
 * cookie, HttpOnly (see `sessionCookieOptions` below), never the header.
 */
export const SESSION_COOKIE_NAME = "vigil_dashboard_session";

/**
 * Cookie attributes for the session cookie, set by
 * app/api/auth/login/route.ts and cleared by app/api/auth/logout/route.ts.
 * HttpOnly (this cookie's value must never be readable by browser JS --
 * XSS should not be able to exfiltrate a live session), Secure in
 * production only (local dev has no TLS to require), SameSite=Strict (this
 * dashboard has no legitimate cross-site navigation that should ever carry
 * the cookie), Path=/ (every dashboard route needs it, not one subtree).
 * `expires` is set to the session's own `expires_at` from apps/api, so the
 * cookie's lifetime always matches the server-side session it represents
 * exactly, rather than duplicating that TTL as separate dashboard config.
 */
export function sessionCookieOptions(expiresAt: string) {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict" as const,
    path: "/",
    expires: new Date(expiresAt),
  };
}

function requireApiBaseUrl(): string {
  const value = process.env.VIGIL_API_BASE_URL;
  if (!value) {
    throw new Error(
      "VIGIL_API_BASE_URL is not configured. Set it as a server-side environment variable " +
        "before starting the dashboard.",
    );
  }
  return value;
}

async function toVigilApiError(response: Response): Promise<VigilApiError> {
  let detail: string | undefined;
  try {
    const body = (await response.json()) as ApiErrorBody;
    detail = extractDetailMessage(body.detail);
  } catch {
    // Non-JSON error body -- fall through to the generic message below.
  }
  return new VigilApiError(response.status, detail ?? "The authentication service returned an error.");
}

export interface LoginResult {
  sessionToken: string;
  expiresAt: string;
}

export interface SessionInfo {
  userId: string;
  email: string;
  expiresAt: string;
}

/**
 * POST /v1/auth/login. Throws VigilApiError(401, "Invalid email or
 * password.") on any failure -- apps/api never distinguishes unknown
 * email / wrong password / inactive account / missing membership in its
 * response, and this function passes that generic message straight
 * through rather than adding its own guesswork on top.
 */
export async function login(email: string, password: string): Promise<LoginResult> {
  const baseUrl = requireApiBaseUrl();

  let response: Response;
  try {
    response = await fetch(`${baseUrl.replace(/\/+$/, "")}/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
      cache: "no-store",
    });
  } catch {
    console.error("dashboard auth: network error calling /v1/auth/login");
    throw new VigilApiError(503, "Unable to reach the authentication service. Please retry.");
  }

  if (!response.ok) {
    throw await toVigilApiError(response);
  }

  const body = (await response.json()) as { session_token: string; expires_at: string };
  return { sessionToken: body.session_token, expiresAt: body.expires_at };
}

/**
 * GET /v1/auth/session. Returns null for any invalid/expired/revoked
 * session -- never throws for that expected, routine outcome (an
 * unauthenticated visitor is not an error condition) -- only for a
 * genuine network/service failure.
 */
export async function validateSession(sessionToken: string): Promise<SessionInfo | null> {
  const baseUrl = requireApiBaseUrl();

  let response: Response;
  try {
    response = await fetch(`${baseUrl.replace(/\/+$/, "")}/v1/auth/session`, {
      method: "GET",
      headers: { [SESSION_TOKEN_HEADER]: sessionToken },
      cache: "no-store",
    });
  } catch {
    console.error("dashboard auth: network error calling /v1/auth/session");
    return null;
  }

  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    console.error(`dashboard auth: /v1/auth/session responded ${response.status}`);
    return null;
  }

  const body = (await response.json()) as { user_id: string; email: string; expires_at: string };
  return { userId: body.user_id, email: body.email, expiresAt: body.expires_at };
}

/**
 * POST /v1/auth/logout. Idempotent on the server side (see
 * apps/api/app/api/v1/auth.py's `logout` docstring) -- this function
 * mirrors that by never throwing for a missing/unknown/already-revoked
 * token, only for a genuine network/service failure, which it swallows
 * too: a failed logout call must never prevent the dashboard's own cookie
 * from being cleared (see app/api/auth/logout/route.ts).
 */
export async function logout(sessionToken: string): Promise<void> {
  const baseUrl = requireApiBaseUrl();

  try {
    await fetch(`${baseUrl.replace(/\/+$/, "")}/v1/auth/logout`, {
      method: "POST",
      headers: { [SESSION_TOKEN_HEADER]: sessionToken },
      cache: "no-store",
    });
  } catch {
    console.error("dashboard auth: network error calling /v1/auth/logout");
  }
}
