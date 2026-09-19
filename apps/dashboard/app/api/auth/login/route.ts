import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { login, SESSION_COOKIE_NAME, sessionCookieOptions } from "@/lib/api/dashboardAuth";

import { handleVigilError } from "../../vigil/_lib/handleError";

/**
 * POST /api/auth/login -- this app's own login route (Phase 4D, F1), not to
 * be confused with apps/api's `POST /v1/auth/login` that
 * lib/api/dashboardAuth.ts's `login()` calls. This route's entire job is
 * the browser-facing half of that call: accept `{ email, password }`,
 * forward it server-side, and turn a successful result into an HttpOnly
 * session cookie -- the raw session token is never put in the JSON
 * response body, only in `Set-Cookie`, so it never reaches JS-accessible
 * storage.
 *
 * proxy.ts leaves this route (and `POST /api/auth/logout`) unauthenticated
 * -- see its own `isPublicPath`.
 *
 * **Requires `Content-Type: application/json`.** This isn't a body-parsing
 * nicety -- SameSite=Strict (see `sessionCookieOptions`) stops a cross-site
 * page from ever getting *this app's* cookie sent back to it, but it does
 * nothing to stop a cross-site page from causing the browser to *send* a
 * request here in the first place, or from *receiving and storing* whatever
 * `Set-Cookie` comes back. A plain `<form enctype="text/plain">` can be
 * crafted so its `text/plain`-encoded body is valid JSON (a known
 * SameSite-bypass technique for JSON APIs), which would otherwise let a
 * cross-site page silently log a visiting browser into an attacker-chosen
 * account ("login CSRF") -- `request.json()` below parses any body that
 * happens to be valid JSON regardless of its declared content type, so the
 * content type must be checked explicitly, before parsing, to close that
 * gap. A real `fetch()` call from this app's own same-origin LoginForm
 * always sends this header, so this rejects nothing legitimate.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("application/json")) {
    return NextResponse.json({ detail: "Invalid request body." }, { status: 400 });
  }

  let email: unknown;
  let password: unknown;
  try {
    const body = (await request.json()) as { email?: unknown; password?: unknown };
    email = body.email;
    password = body.password;
  } catch {
    return NextResponse.json({ detail: "Invalid request body." }, { status: 400 });
  }

  if (typeof email !== "string" || typeof password !== "string" || !email || !password) {
    return NextResponse.json({ detail: "Email and password are required." }, { status: 400 });
  }

  try {
    const result = await login(email, password);
    const response = NextResponse.json({ ok: true });
    response.cookies.set(SESSION_COOKIE_NAME, result.sessionToken, sessionCookieOptions(result.expiresAt));
    return response;
  } catch (error) {
    return handleVigilError(error);
  }
}
