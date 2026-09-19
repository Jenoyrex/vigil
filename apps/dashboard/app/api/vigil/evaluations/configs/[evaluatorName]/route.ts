import { NextRequest, NextResponse } from "next/server";

import { getEvaluatorConfig, upsertEvaluatorConfig } from "@/lib/api/evaluations";
import type { EvaluatorConfigUpsertRequest } from "@/lib/api/types";

import { handleVigilError } from "../../../_lib/handleError";

/** Same-origin proxy for GET /v1/evaluations/configs/{evaluator_name}. */
export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ evaluatorName: string }> },
): Promise<NextResponse> {
  const { evaluatorName } = await params;
  try {
    const data = await getEvaluatorConfig(evaluatorName);
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}

/**
 * Same-origin proxy for PUT /v1/evaluations/configs/{evaluator_name} --
 * the one write path this dashboard has. The request body is forwarded
 * as-is (never validated or defaulted here -- that's the upstream API's
 * job, per this app's thin-proxy convention); a malformed body falls
 * through to the same `handleVigilError` catch every other route already
 * uses, so it never surfaces as an unhandled 500 with a stack trace.
 */
export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ evaluatorName: string }> },
): Promise<NextResponse> {
  const { evaluatorName } = await params;
  try {
    const body = (await request.json()) as EvaluatorConfigUpsertRequest;
    const data = await upsertEvaluatorConfig(evaluatorName, body);
    return NextResponse.json(data);
  } catch (error) {
    return handleVigilError(error);
  }
}
