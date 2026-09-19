import { NextResponse } from "next/server";

import { listEvaluatorConfigs } from "@/lib/api/evaluations";

import { handleVigilError } from "../../_lib/handleError";

/** Same-origin proxy for GET /v1/evaluations/configs. No query parameters. */
export async function GET(): Promise<NextResponse> {
  try {
    const data = await listEvaluatorConfigs();
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
