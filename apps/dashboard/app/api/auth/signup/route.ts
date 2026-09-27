import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { deriveClientIp, parseTrustedProxyHops } from "@/lib/api/clientIp";
import { PROJECT_COOKIE_NAME, SESSION_COOKIE_NAME, sessionCookieOptions, signup } from "@/lib/api/dashboardAuth";

import { handleVigilError } from "../../vigil/_lib/handleError";

/**
 * POST /api/auth/signup -- the browser-facing half of apps/api's
 * `POST /v1/auth/signup`, shaped exactly like ./../login/route.ts: JSON
 * only (the same login-CSRF reasoning applies -- a cross-site form must not
 * be able to create an account and plant its session in a visitor's
 * browser), and the session token only ever lands in the HttpOnly cookie.
 * Password rules and duplicate-email checks are apps/api's; its messages
 * pass straight through.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("application/json")) {
    return NextResponse.json({ detail: "Invalid request body." }, { status: 400 });
  }

  let body: { email?: unknown; password?: unknown; fullName?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ detail: "Invalid request body." }, { status: 400 });
  }

  const { email, password, fullName } = body;
  if (typeof email !== "string" || typeof password !== "string" || !email || !password) {
    return NextResponse.json({ detail: "Email and password are required." }, { status: 400 });
  }

  try {
    const clientIp = deriveClientIp(
      request.headers.get("x-forwarded-for"),
      parseTrustedProxyHops(process.env.VIGIL_DASHBOARD_TRUSTED_PROXY_HOPS),
    );
    const result = await signup(
      {
        email,
        password,
        ...(typeof fullName === "string" && fullName.trim() ? { full_name: fullName.trim() } : {}),
      },
      clientIp,
    );
    const response = NextResponse.json({ ok: true }, { status: 201 });
    // A previous user's project selection must not carry into this session.
    response.cookies.delete(PROJECT_COOKIE_NAME);
    response.cookies.set(SESSION_COOKIE_NAME, result.sessionToken, sessionCookieOptions(result.expiresAt));
    return response;
  } catch (error) {
    return handleVigilError(error);
  }
}
