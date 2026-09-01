import { NextRequest, NextResponse } from "next/server";

import { getLlmUsageAnalytics } from "@/lib/api/analytics";
import type { LlmGroupBy } from "@/lib/api/types";

import { handleVigilError } from "../../_lib/handleError";

/** Same-origin proxy for GET /v1/analytics/llm-usage. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const params = request.nextUrl.searchParams;
  try {
    const data = await getLlmUsageAnalytics({
      start_time_from: params.get("start_time_from") ?? undefined,
      start_time_to: params.get("start_time_to") ?? undefined,
      environment: params.get("environment") ?? undefined,
      group_by: (params.get("group_by") as LlmGroupBy | null) ?? undefined,
    });
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
