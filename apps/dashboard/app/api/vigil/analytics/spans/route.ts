import { NextRequest, NextResponse } from "next/server";

import { getSpanAnalytics } from "@/lib/api/analytics";
import type { SpanBucket, SpanGroupBy } from "@/lib/api/types";

import { handleVigilError } from "../../_lib/handleError";

/** Same-origin proxy for GET /v1/analytics/spans. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const params = request.nextUrl.searchParams;
  try {
    const data = await getSpanAnalytics({
      start_time_from: params.get("start_time_from") ?? undefined,
      start_time_to: params.get("start_time_to") ?? undefined,
      environment: params.get("environment") ?? undefined,
      resource: params.get("resource") ?? undefined,
      span_type: params.get("span_type") ?? undefined,
      group_by: (params.get("group_by") as SpanGroupBy | null) ?? undefined,
      bucket: (params.get("bucket") as SpanBucket | null) ?? undefined,
    });
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
