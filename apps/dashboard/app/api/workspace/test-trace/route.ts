import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { apiFetch } from "@/lib/api/http";

import { handleVigilError } from "../../vigil/_lib/handleError";
import { badRequest, readJson, requiredString } from "../_lib";

function randomHex(bytes: number): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
}

/**
 * POST /api/workspace/test-trace -- onboarding's "Send a test trace": sends
 * one real `llm` span through the real ingestion endpoint
 * (POST /v1/traces), authenticated by the API key the user just created
 * and still has on screen. Nothing is written anywhere else: the trace
 * lands in ClickHouse like any SDK trace, and the evaluation pipeline
 * picks it up like any other. It is labeled as a test (name, service,
 * environment) and claims no model or token usage it didn't have.
 *
 * The key only passes through this server for this one request and is
 * never logged or stored (lib/api/http.ts never logs headers).
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const parsed = await readJson(request);
  if ("error" in parsed) return parsed.error;
  const apiKey = requiredString(parsed.body, "apiKey");
  if (!apiKey || !/^vgl_[0-9a-f]+\.[A-Za-z0-9_-]+$/.test(apiKey)) {
    return badRequest("Paste a Vigil API key (vgl_…) to send a test trace.");
  }

  const end = new Date();
  const start = new Date(end.getTime() - 850);
  const traceId = randomHex(16);
  try {
    await apiFetch("/v1/traces", undefined, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}` },
      body: {
        resource: { "service.name": "vigil-onboarding", "sdk.name": "vigil-dashboard-test" },
        spans: [
          {
            trace_id: traceId,
            span_id: randomHex(8),
            parent_span_id: null,
            name: "onboarding test trace",
            span_type: "llm",
            start_time: start.toISOString(),
            end_time: end.toISOString(),
            status: "ok",
            input: "What does Vigil do for an LLM application?",
            output:
              "Vigil records traces from an LLM application and evaluates whether each response is relevant to its input.",
            environment: "onboarding-test",
          },
        ],
      },
    });
    return NextResponse.json({ traceId }, { status: 201 });
  } catch (error) {
    return handleVigilError(error);
  }
}
