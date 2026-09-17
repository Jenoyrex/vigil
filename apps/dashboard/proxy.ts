import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { getTrace } from "@/lib/api/traces";
import { deriveStartDate } from "@/lib/traceStartDate";
import { VigilApiError } from "@/lib/api/types";

// Mirrors the CSP this app has always sent (see
// docs/decisions/007-cors-and-dashboard-security-headers.md) with one
// change: `script-src` now carries a per-request nonce instead of being a
// bare `'self'`. A bare `script-src 'self'` -- no `'unsafe-inline'`, no
// nonce -- silently blocks the inline `$RC(...)` script Next.js's own
// Suspense streaming injects into the HTML to complete a deferred boundary
// client-side; the browser drops the script, the boundary's completion
// signal never arrives, and React reports that as a closed connection
// (minified error #412). Next.js automatically stamps this nonce onto every
// script it generates -- framework runtime, page bundles, and this
// streaming-completion script included -- once it sees the value in the CSP
// header on the (proxy-forwarded) request; see "How nonces work in
// Next.js" in Next's CSP guide
// (node_modules/next/dist/docs/01-app/02-guides/content-security-policy.md
// for this exact installed version). No other directive changes: style-src
// keeps 'unsafe-inline' (next/font's inline @font-face style, Recharts'
// inline SVG styling -- neither is user-controlled), and everything else is
// untouched.
function buildContentSecurityPolicy(nonce: string): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}'`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");
}

/**
 * Pre-render existence check for the trace-detail page, plus this app's CSP
 * nonce generation (see `buildContentSecurityPolicy` above).
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
  // A nonce only has security value if it's unpredictable and used exactly
  // once, so it's generated fresh on every invocation of this function --
  // never hoisted to module scope, where it would be reused across every
  // request this server process handles. Scoped to production only, same as
  // this app's CSP always has been: `next dev`'s Turbopack HMR client relies
  // on eval-ish module loading a strict script-src would break, and there's
  // no security benefit to enforcing this in a local dev loop no browser
  // outside the developer's own machine ever reaches.
  let init: { request: { headers: Headers } } | undefined;
  let nonce: string | undefined;
  if (process.env.NODE_ENV === "production") {
    nonce = Buffer.from(crypto.randomUUID()).toString("base64");
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set("x-nonce", nonce);
    requestHeaders.set("Content-Security-Policy", buildContentSecurityPolicy(nonce));
    init = { request: { headers: requestHeaders } };
  }

  const match = /^\/traces\/([^/]+)$/.exec(request.nextUrl.pathname);
  let response: NextResponse;

  if (!match) {
    response = NextResponse.next(init);
  } else {
    const traceId = match[1];
    const startParam = request.nextUrl.searchParams.get("start") ?? undefined;

    try {
      await getTrace(traceId, { start_date: deriveStartDate(startParam) });
      response = NextResponse.next(init);
    } catch (error) {
      if (error instanceof VigilApiError && error.status === 404) {
        response = NextResponse.rewrite(new URL("/trace-not-found", request.url), init);
      } else {
        // Malformed id (422), upstream errors (503/500), network failures:
        // let the existing page render its existing ErrorBanner handling
        // unchanged.
        response = NextResponse.next(init);
      }
    }
  }

  // NextResponse.next()/rewrite() only apply `init.request.headers` to the
  // request Next.js renders with (so it can discover the nonce) -- the
  // response actually sent to the browser needs the same header set
  // explicitly.
  if (nonce) {
    response.headers.set("Content-Security-Policy", buildContentSecurityPolicy(nonce));
  }

  return response;
}

export const config = {
  matcher: [
    {
      // Same page routes this app has always sent security headers to,
      // minus the ones that never need a CSP: API responses aren't HTML,
      // and static/image assets and prefetch requests don't run scripts.
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
