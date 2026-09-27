import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * JSON-only body parsing for the /api/workspace/** mutations -- the same
 * explicit content-type check as app/api/auth/login/route.ts, so a
 * cross-site `<form enctype="text/plain">` can never drive them. Returns a
 * ready 400 response instead of a body when the request isn't JSON.
 */
export async function readJson(
  request: NextRequest,
): Promise<{ body: Record<string, unknown> } | { error: NextResponse }> {
  const contentType = request.headers.get("content-type") ?? "";
  if (contentType.toLowerCase().startsWith("application/json")) {
    try {
      const body: unknown = await request.json();
      if (body && typeof body === "object" && !Array.isArray(body)) {
        return { body: body as Record<string, unknown> };
      }
    } catch {
      // fall through
    }
  }
  return { error: NextResponse.json({ detail: "Invalid request body." }, { status: 400 }) };
}

export function requiredString(body: Record<string, unknown>, key: string): string | null {
  const value = body[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function badRequest(detail: string): NextResponse {
  return NextResponse.json({ detail }, { status: 400 });
}
