import type { NextConfig } from "next";

// Strict-Transport-Security is deliberately scoped to production builds
// only (see docs/decisions/007-cors-and-dashboard-security-headers.md). The
// other headers below (X-Content-Type-Options, Referrer-Policy,
// Permissions-Policy, X-Frame-Options) are harmless in dev and apply
// unconditionally.
//
// Content-Security-Policy is NOT set here: it lives in proxy.ts instead,
// because Next.js can only stamp its own Suspense-streaming completion
// scripts with a CSP nonce it discovers on the incoming request, and a
// nonce -- unique per response -- can only be generated per-request, not in
// this static config. See proxy.ts for the full CSP string and the CSP
// nonce fix for React error #412 ("Connection closed") it exists to
// resolve.
const isProduction = process.env.NODE_ENV === "production";

const nextConfig: NextConfig = {
  // Phase 4B (deployment): a self-contained production server bundle
  // (.next/standalone), required for the lean multi-stage Docker image in
  // apps/dashboard/Dockerfile.
  output: "standalone",

  // Phase 4C: baseline production security headers. See
  // docs/decisions/007-cors-and-dashboard-security-headers.md for the full
  // rationale behind each header and each CSP directive.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          // Only the browser features this app has zero use for -- not an
          // attempt to enumerate every possible permission.
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
          // Kept alongside CSP's frame-ancestors (below) for older
          // browsers that don't support frame-ancestors -- redundant on
          // modern browsers, but that redundancy is the point, not an
          // oversight.
          { key: "X-Frame-Options", value: "DENY" },
          ...(isProduction
            ? [
                // Inert until an operator's TLS-terminating reverse proxy
                // sits in front of this dashboard (see ADR 006's "No TLS
                // termination" known limitation): browsers only honor
                // Strict-Transport-Security received over an
                // already-HTTPS connection, so sending it over the plain
                // HTTP this app serves directly today is spec-defined to
                // be a no-op, and takes effect automatically the moment
                // TLS termination is added, with no further app change.
                // No `includeSubDomains`, no `preload` -- both are
                // domain-wide commitments this app has no authority to
                // make. Vigil is self-hosted: an operator can deploy this
                // dashboard under any domain they choose, and neither this
                // codebase nor ADR 006 states or assumes what that domain
                // is, whether it's shared with apps/api, or what else (a
                // completely unrelated service on a sibling subdomain,
                // outside this app's knowledge or control) might be
                // running on a sibling subdomain of it. includeSubDomains
                // would force a browser to enforce HTTPS on every such
                // subdomain for the full max-age, which this app cannot
                // respect or reason about. preload is the same class of
                // decision, one step further. Both are things an operator
                // who actually controls the domain can add themselves
                // (e.g. at their reverse proxy) once they've confirmed
                // every subdomain is HTTPS-only -- not something to send
                // unconditionally on their behalf.
                {
                  key: "Strict-Transport-Security",
                  value: "max-age=15552000",
                },
              ]
            : []),
        ],
      },
    ];
  },
};

export default nextConfig;
