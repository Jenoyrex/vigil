import { NextResponse } from "next/server";

import { VigilApiError } from "@/lib/api/types";

/**
 * Maps a lib/api/* failure to a same-origin JSON error response, preserving
 * the upstream status code (401/404/422/503/etc.) exactly. Only ever
 * forwards the upstream's own `detail` message -- never a stack trace, a
 * raw error object, or any header. This is the single place every proxy
 * route handler turns a failure into a response, so that guarantee only
 * has to be gotten right once.
 */
export function handleVigilError(error: unknown): NextResponse {
  if (error instanceof VigilApiError) {
    return NextResponse.json({ detail: error.message }, { status: error.status });
  }
  console.error("vigil proxy: unexpected error");
  return NextResponse.json({ detail: "An unexpected error occurred." }, { status: 500 });
}
