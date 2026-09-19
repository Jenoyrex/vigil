import { NextRequest, NextResponse } from "next/server";

import { listEvaluationJobs } from "@/lib/api/evaluations";
import type { EvaluationJobStatus } from "@/lib/api/types";

import { handleVigilError } from "../../_lib/handleError";

/** Same-origin proxy for GET /v1/evaluations/jobs. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const params = request.nextUrl.searchParams;
  try {
    const data = await listEvaluationJobs({
      status: (params.get("status") as EvaluationJobStatus | null) ?? undefined,
      evaluator_name: params.get("evaluator_name") ?? undefined,
      limit: params.has("limit") ? Number(params.get("limit")) : undefined,
      cursor: params.get("cursor") ?? undefined,
    });
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
