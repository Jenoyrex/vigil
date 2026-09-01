import { NextRequest, NextResponse } from "next/server";

import { getTrace } from "@/lib/api/traces";

import { handleVigilError } from "../../_lib/handleError";

/** Same-origin proxy for GET /v1/traces/{trace_id}. */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ traceId: string }> },
): Promise<NextResponse> {
  const { traceId } = await params;
  const startDate = request.nextUrl.searchParams.get("start_date") ?? undefined;
  try {
    const data = await getTrace(traceId, { start_date: startDate });
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
