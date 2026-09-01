import { NextRequest, NextResponse } from "next/server";

import { listTraces } from "@/lib/api/traces";

import { handleVigilError } from "../_lib/handleError";

/**
 * Same-origin proxy for GET /v1/traces. The browser calls this path (never
 * the real Vigil API); this handler runs server-side, so
 * `listTraces` -> `vigilFetch` attaches the server-only API key here,
 * never in browser-visible code.
 */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const params = request.nextUrl.searchParams;
  try {
    const data = await listTraces({
      start_time_from: params.get("start_time_from") ?? undefined,
      start_time_to: params.get("start_time_to") ?? undefined,
      environment: params.get("environment") ?? undefined,
      resource: params.get("resource") ?? undefined,
      has_error: params.has("has_error") ? params.get("has_error") === "true" : undefined,
      limit: params.has("limit") ? Number(params.get("limit")) : undefined,
      cursor: params.get("cursor") ?? undefined,
    });
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
