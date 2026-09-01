import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { getTrace } from "@/lib/api/traces";
import { deriveStartDate } from "@/lib/traceStartDate";
import { VigilApiError } from "@/lib/api/types";

/**
 * Pre-render existence check for the trace-detail page.
 *
 * `app/traces/[traceId]/page.tsx` has a sibling `loading.tsx`, so Next.js
 * wraps its render (and that of any nested segment under it) in an
 * implicit Suspense boundary; by the time the page's own `notFound()`
 * call runs, the response has already started streaming as a 200, and
 * the status can't change (this is a documented Next.js trade-off -- see
 * the `notFound()` API reference, "Calling notFound() after streaming has
 * started"). Proxy runs before any of that rendering begins, so it's the
 * only place left that can still decide the response status.
 *
 * A confirmed-missing trace is rewritten to app/trace-not-found, a
 * segment deliberately kept outside app/traces/** so it has no
 * loading.tsx anywhere in its ancestry -- its `notFound()` call runs
 * before any streaming starts, producing a real HTTP 404, resolved to
 * the same "Trace not found" UI (see app/trace-not-found/not-found.tsx).
 * Every other outcome (found, malformed id, upstream error) falls
 * through unchanged to the existing page, which already handles it.
 */
export async function proxy(request: NextRequest) {
  const match = /^\/traces\/([^/]+)$/.exec(request.nextUrl.pathname);
  if (!match) return NextResponse.next();
  const traceId = match[1];

  const startParam = request.nextUrl.searchParams.get("start") ?? undefined;

  try {
    await getTrace(traceId, { start_date: deriveStartDate(startParam) });
  } catch (error) {
    if (error instanceof VigilApiError && error.status === 404) {
      return NextResponse.rewrite(new URL("/trace-not-found", request.url));
    }
    // Malformed id (422), upstream errors (503/500), network failures: let
    // the existing page render its existing ErrorBanner handling unchanged.
  }

  return NextResponse.next();
}

export const config = {
  matcher: "/traces/:traceId",
};
