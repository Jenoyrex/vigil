import type { NextConfig } from "next";

// Content-Security-Policy is deliberately scoped to production builds only
// (see docs/decisions/007-cors-and-dashboard-security-headers.md): `next
// dev`'s Turbopack HMR client relies on eval-ish module loading a strict
// script-src would break, and there is no security benefit to enforcing
// this in a local dev loop no browser outside the developer's own machine
// ever reaches. The other headers below (X-Content-Type-Options,
// Referrer-Policy, Permissions-Policy, X-Frame-Options) are harmless in
// dev and apply unconditionally.
const isProduction = process.env.NODE_ENV === "production";

// Verified against this app's actual code, not assumed:
// - script-src has no 'unsafe-inline'/'unsafe-eval' -- there is no inline
//   <script>, no dangerouslySetInnerHTML, and no eval()/Function() anywhere
//   in apps/dashboard.
// - style-src needs 'unsafe-inline': app/layout.tsx's next/font/google
//   (Geist/Geist_Mono) injects an inline <style> tag with @font-face rules
//   to avoid FOUC (this is how next/font works, not an app choice), and
//   components/analytics/TimeSeriesChart.tsx (the one place Recharts is
//   used) sets inline `style` attributes on its SVG/Tooltip elements for
//   computed positioning and theming. Both are same-origin,
//   framework/library-owned styles, never user-controlled content -- the
//   realistic risk 'unsafe-inline' style-src accepts here is CSS-based
//   exfiltration/defacement, not arbitrary script execution.
// - font-src is 'self' only: next/font self-hosts Google Fonts at build
//   time (no runtime request to fonts.gstatic.com).
// - connect-src is 'self' only: every client-side fetch in this app
//   targets its own same-origin /api/vigil/** BFF proxy routes
//   (lib/api/browserClient.ts) -- never an external endpoint. Server
//   Components' own direct calls to the real Vigil API
//   (lib/api/vigilClient.ts) never go through the browser at all, so CSP
//   (a browser-enforced mechanism) doesn't apply to them.
// - img-src is 'self': no <img>/next/image usage and no public/ assets
//   exist in this app today.
const contentSecurityPolicy = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self'",
  "font-src 'self'",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

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
                { key: "Content-Security-Policy", value: contentSecurityPolicy },
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
