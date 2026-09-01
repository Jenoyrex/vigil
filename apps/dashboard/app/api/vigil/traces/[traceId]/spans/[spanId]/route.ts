import { NextRequest, NextResponse } from "next/server";

import { getSpan } from "@/lib/api/traces";

import { handleVigilError } from "../../../../_lib/handleError";

/**
 * Same-origin proxy for GET /v1/traces/{trace_id}/spans/{span_id}.
 *
 * Only called by the dashboard for the deep-link fallback (a `?span=` id
 * not present in an already-loaded, possibly-truncated trace) -- see
 * lib/api/traces.ts's `getSpan` docstring.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ traceId: string; spanId: string }> },
): Promise<NextResponse> {
  const { traceId, spanId } = await params;
  const startDate = request.nextUrl.searchParams.get("start_date") ?? undefined;
  try {
    const data = await getSpan(traceId, spanId, { start_date: startDate });
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
