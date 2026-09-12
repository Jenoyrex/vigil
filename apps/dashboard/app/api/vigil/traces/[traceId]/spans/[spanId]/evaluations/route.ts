import { NextRequest, NextResponse } from "next/server";

import { getSpanEvaluations } from "@/lib/api/evaluations";

import { handleVigilError } from "../../../../../_lib/handleError";

/**
 * Same-origin proxy for GET /v1/traces/{trace_id}/spans/{span_id}/evaluations.
 *
 * Called by SpanEvaluationsPanel for the currently-selected span only --
 * never once per span in a Waterfall (there is no trace-scoped "all
 * evaluations" endpoint to batch this against).
 */
export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ traceId: string; spanId: string }> },
): Promise<NextResponse> {
  const { traceId, spanId } = await params;
  try {
    const data = await getSpanEvaluations(traceId, spanId);
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
