import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { getTrace } from "@/lib/api/traces";
import { deriveStartDate } from "@/lib/traceStartDate";
import { VigilApiError } from "@/lib/api/types";
import { SESSION_COOKIE_NAME, validateSession } from "@/lib/api/dashboardAuth";

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

const LOGIN_PATH = "/login";

/**
 * Reachable without a valid dashboard session (Phase 4D, F1): `/login`
 * itself, and this app's own `/api/auth/**` routes (login/logout), which
 * obviously cannot themselves require the session they establish or tear
 * down. Every other path -- every page, and every `/api/vigil/**` BFF
 * route -- requires one; see the session gate in `proxy()` below.
 */
function isPublicPath(pathname: string): boolean {
  return pathname === LOGIN_PATH || pathname.startsWith("/api/auth/");
}

/**
 * Session gate (Phase 4D, F1). Calls apps/api's `GET /v1/auth/session` on
 * every gated request -- no caching layer on either side, matching that
 * endpoint's own design (see app/api/v1/auth.py's module docstring), so a
 * revoked or expired session is rejected on its very next request here
 * too, not just at the API. The cookie itself is HttpOnly (see
 * lib/api/dashboardAuth.ts's `sessionCookieOptions`), so this is also the
 * only place that ever reads its value.
 */
async function hasValidSession(request: NextRequest): Promise<boolean> {
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return false;
  return (await validateSession(token)) !== null;
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
  const pathname = request.nextUrl.pathname;
  const isApiRoute = pathname.startsWith("/api/");

  if (!isPublicPath(pathname) && !(await hasValidSession(request))) {
    if (isApiRoute) {
      return NextResponse.json({ detail: "Invalid or expired session." }, { status: 401 });
    }
    const loginUrl = new URL(LOGIN_PATH, request.url);
    // pathname + search (never request.nextUrl.href or .toString()) -- only
    // the same-origin path-and-query ever goes into `next`, so there is
    // nothing here for app/login/page.tsx's sanitizeNextPath to see other
    // than a same-origin relative reference, regardless of what this
    // request's own URL looked like.
    loginUrl.searchParams.set("next", pathname + request.nextUrl.search);
    return NextResponse.redirect(loginUrl);
  }

  // CSP nonce generation and the trace-detail 404 rewrite below only apply
  // to HTML page routes -- `/api/**` responses are JSON, never rendered,
  // and never need either.
  if (isApiRoute) {
    return NextResponse.next();
  }

  // A client-side router prefetch (`next-router-prefetch`/`purpose:
  // prefetch`) never runs scripts or needs the 404 rewrite, so both are
  // skipped for one as a performance optimization only -- unlike the old
  // matcher-level `missing` filter this replaces, the session gate above
  // already ran unconditionally before this point, since both headers are
  // attacker-controlled and must never be able to skip authentication.
  const isPrefetch =
    request.headers.has("next-router-prefetch") || request.headers.get("purpose") === "prefetch";

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
  if (process.env.NODE_ENV === "production" && !isPrefetch) {
    nonce = Buffer.from(crypto.randomUUID()).toString("base64");
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set("x-nonce", nonce);
    requestHeaders.set("Content-Security-Policy", buildContentSecurityPolicy(nonce));
    init = { request: { headers: requestHeaders } };
  }

  const match = isPrefetch ? null : /^\/traces\/([^/]+)$/.exec(pathname);
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
  // Deliberately no `missing`/`has` filter on router-prefetch or `purpose:
  // prefetch` headers here (unlike before Phase 4D F1): both are ordinary,
  // attacker-settable request headers, and this matcher now also gates
  // whether the session check runs at all -- filtering proxy invocation on
  // either would let a crafted request skip authentication entirely by
  // simply presenting one. `isPrefetch` inside `proxy()` still skips the
  // (non-security) CSP-nonce/trace-check work for a genuine prefetch, using
  // the same signal safely, since skipping optional work is not a security
  // decision the way skipping the auth check would be.
  matcher: [
    // Every page route (minus static/image assets, which never need a
    // session or a CSP) and every `/api/**` route (this app's `/api/vigil/**`
    // BFF proxy and `/api/auth/**` login/logout) -- `/api/auth/**` and
    // `/login` are carved back out as public paths inside `proxy()` itself.
    "/((?!_next/static|_next/image|favicon.ico).*)",
  ],
};
