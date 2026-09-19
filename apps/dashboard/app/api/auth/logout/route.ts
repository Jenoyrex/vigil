import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { logout, SESSION_COOKIE_NAME } from "@/lib/api/dashboardAuth";

/**
 * POST /api/auth/logout (Phase 4D, F1). Idempotent, mirroring apps/api's
 * own `POST /v1/auth/logout`: a missing cookie is treated as already
 * logged out, not an error. The cookie is cleared unconditionally either
 * way -- a failed upstream revocation call (see `logout()`'s own
 * docstring, which swallows network errors) must never leave a dead
 * cookie behind that the browser keeps sending.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (token) {
    await logout(token);
  }

  const response = new NextResponse(null, { status: 204 });
  response.cookies.delete(SESSION_COOKIE_NAME);
  return response;
}
