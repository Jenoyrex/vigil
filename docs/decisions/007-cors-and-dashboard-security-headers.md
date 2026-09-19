# 7. CORS and Dashboard Security Headers

- Status: Accepted
- Date: 2026-09-13

## Context

Phase 4C's approved production-readiness scope included two remaining P2 hardening items from the
independent audit: no explicit CORS policy on `apps/api` (M1), and no security headers on
`apps/dashboard` (M2). Both were deferred past Commits 1-3 (CI, SDK retry hardening, per-API-key
rate limiting) and are the subject of this ADR.

## Decision

### 1. CORS: explicit deny-by-default, not a permissive default

`apps/api` previously had zero CORS configuration -- no `CORSMiddleware`, not even a restrictive
one, meaning the *absence* of a policy was itself the (undocumented) policy. This ADR adds an
explicit `CORSMiddleware` (`app/main.py`), configured from a new `VIGIL_API_CORS_ALLOWED_ORIGINS`
setting (`app/config.py`), **empty by default**.

Empty is the correct default, not a gap to fill in later: this API's only real consumers today are
`packages/sdk-python` (a non-browser HTTP client -- CORS is a browser-only enforcement mechanism
and does not apply to it at all) and `apps/dashboard`'s own server process, which calls this API
directly, server-to-server (`apps/dashboard/lib/api/vigilClient.ts`, `import "server-only"`) and
never exposes it to browser JavaScript. There is no legitimate browser-based cross-origin consumer
of this API to accommodate. If one is introduced in the future,
`VIGIL_API_CORS_ALLOWED_ORIGINS` accepts a comma-separated list of exact origins (scheme + host +
port); `Settings.cors_allowed_origins_list` parses and validates this, and **raises at startup** if
it ever contains a literal `*` -- wildcard CORS is not supported by this API at all, even if
manually typed into the environment, so a dangerous misconfiguration fails loudly at process start
rather than quietly granting every origin cross-origin access.

`allow_credentials` is always `false`. This API authenticates via `Authorization: Bearer
<api-key>`, never cookies, so credentialed CORS has no purpose here -- and per the Fetch/CORS spec,
`allow_credentials=true` combined with a wildcard origin is a well-known misconfiguration class;
setting it to `false` unconditionally removes that risk category entirely rather than relying on
correct configuration to avoid it.

`allow_methods` is `["GET", "POST", "PUT"]` and `allow_headers` is `["Authorization",
"Content-Type"]` -- exactly what this API's routes actually use (per `apps/api/app/api/v1/*.py`),
not `["*"]`. (Starlette's `CORSMiddleware` additionally always permits the CORS-safelisted request
headers -- `Accept`, `Accept-Language`, `Content-Language` -- per the Fetch spec; those never need
explicit allowance and are not something this configuration controls.)

`CORSMiddleware` is registered globally (Starlette middleware wraps the whole app, with no
per-route CORS exemption mechanism), including `/health`, `/ready`, and
`POST /v1/evaluations/jobs`. This does not make any of them more permissive: CORS is enforced by
the *browser* refusing to let its own JavaScript read a cross-origin response, not by the server
refusing to process the request -- a non-browser caller (Docker's `HEALTHCHECK`, `services/worker`'s
poller, `packages/sdk-python`, `curl`) is entirely unaffected by CORS either way, and a
hypothetical cross-origin browser `fetch()` to any of these routes was already unable to read the
response before this change (no CORS headers existed at all) and remains unable to after (empty
allowlist). `POST /v1/evaluations/jobs` in particular remains governed exclusively by its existing
`X-Vigil-Internal-Token` authentication (`app/api/deps.py::get_internal_service_auth`, unchanged);
CORS is an orthogonal, browser-only concern layered on top, not a substitute for or interaction
with that authentication.

Registered *after* `MaxBodySizeMiddleware` in `app/main.py`, making it the outermost middleware
(Starlette wraps in reverse registration order) -- a cross-origin preflight (`OPTIONS`) request is
answered here first, before it would otherwise reach body-size checks or routing.

### 2. Dashboard security headers, applied via `next.config.ts`'s `headers()`

`apps/dashboard` previously sent no security headers at all. `next.config.ts` now returns a
baseline set for every route (`source: "/:path*"`), verified (not assumed) against this app's
actual code before being written:

- **`X-Content-Type-Options: nosniff`**, **`Referrer-Policy: strict-origin-when-cross-origin`**,
  **`Permissions-Policy: camera=(), microphone=(), geolocation=()`** (this app uses none of those
  three browser features -- not an attempt to enumerate every possible permission),
  **`X-Frame-Options: DENY`** -- applied unconditionally, in every environment. None of these
  interfere with `next dev`.
- **`Content-Security-Policy`** and **`Strict-Transport-Security`** -- applied only when
  `NODE_ENV === "production"`. `next dev`'s Turbopack HMR client relies on eval-ish module loading
  a strict `script-src` would break, and there is no security benefit to enforcing either header in
  a local dev loop no browser outside the developer's own machine ever reaches.

CSP directives and why each is safe for this specific app (inspected before writing, not assumed):

| Directive | Value | Why |
|---|---|---|
| `script-src` | `'self'` | No inline `<script>`, no `dangerouslySetInnerHTML`, no `eval()`/`Function()` anywhere in `apps/dashboard`. |
| `style-src` | `'self' 'unsafe-inline'` | Two real, same-origin, framework/library-owned sources of inline styles: `app/layout.tsx`'s `next/font/google` (Geist/Geist Mono) injects an inline `<style>` tag with `@font-face` rules to avoid FOUC (how `next/font` works, not an app choice), and `components/analytics/TimeSeriesChart.tsx` (the one place Recharts is used, per that file's own "justified only here" comment) sets inline `style` attributes on its SVG/Tooltip elements for computed positioning and theming. Neither is user-controlled content. The realistic risk `'unsafe-inline'` accepts here is scoped to `style-src` only -- CSS-based exfiltration/defacement -- not arbitrary script execution; `script-src` has no such allowance. |
| `img-src` | `'self'` | No `<img>`/`next/image` usage and no `public/` assets exist in this app today. |
| `font-src` | `'self'` | `next/font` self-hosts Google Fonts at build time; no runtime request to `fonts.gstatic.com`. |
| `connect-src` | `'self'` | Every client-side `fetch()` in this app (`lib/api/browserClient.ts`) targets its own same-origin `/api/vigil/**` BFF proxy routes -- never an external endpoint. Server Components' own direct calls to the real Vigil API (`lib/api/vigilClient.ts`) never go through the browser at all, so CSP (a browser-enforced mechanism) doesn't apply to them regardless. |
| `frame-ancestors` | `'none'` | Kept alongside `X-Frame-Options: DENY` for older browsers that don't support `frame-ancestors` -- deliberate redundancy, not an oversight. |
| `base-uri` | `'self'` | Standard hardening against `<base>` tag injection; no legitimate use of a non-default base in this app. |
| `form-action` | `'self'` | The one native `<form>` in this app (`components/evaluations/EvaluatorConfigForm.tsx`) has no `action` attribute at all -- it submits via `onSubmit`/`fetch`, governed by `connect-src`, not `form-action`. Included as defense-in-depth for native submission fallback behavior, not because it's load-bearing today. |
| `object-src` | `'none'` | No Flash/plugin content; universally safe to disable. |

`X-XSS-Protection` is deliberately not set: it is deprecated, ignored by modern browsers, and had a
history of introducing its own XSS vulnerabilities in some engines in the past -- CSP is its
replacement, already present.

**HSTS and the actual HTTPS deployment model.** `docker-compose.prod.yml` publishes `dashboard` as
plain HTTP on host port `3000` -- this app never terminates TLS itself (ADR 006's "No TLS
termination or reverse proxy" known limitation, unchanged). Sending `Strict-Transport-Security`
from a server that only ever speaks plain HTTP directly is safe and intentional, not an oversight:
per the HSTS specification, browsers only store/enforce an `Strict-Transport-Security` header
received over an *already-HTTPS* connection -- received over plain HTTP, it is spec-defined to be
inert. This means the header is harmless today and becomes effective automatically the moment an
operator's TLS-terminating reverse proxy is placed in front of this dashboard, with no further
application change required. `max-age=15552000` (180 days) is used, **without**
`includeSubDomains` and **without** `preload`.

Both omissions were checked against the actual repo, not assumed by default: neither this
codebase nor ADR 006 states or assumes what production hostname/domain Vigil is deployed under, or
whether `apps/dashboard` shares that domain with `apps/api` or anything else -- ADR 006's own "No
TLS termination or reverse proxy" limitation only says TLS termination is "handled by
infrastructure the operator already has in front of this stack," with no domain topology implied
either way. Vigil is self-hosted: an operator can deploy this dashboard under any domain they
choose, and this application has no way to know -- and no business asserting -- whether every
sibling subdomain of that domain is HTTPS-only. `includeSubDomains` would force a browser to
enforce HTTPS on *every* subdomain of that domain for the full `max-age`, including services this
app has never heard of and the operator may run independently (an internal tool, a different
product, a subdomain managed by a different team) -- a domain-wide commitment only the party who
actually controls the domain can make correctly. `preload` (submission to browsers' built-in HSTS
preload list) is the same class of decision, one step further, and normally requires
`includeSubDomains` as a prerequisite in any case. Neither is required for HSTS to function
correctly on this host once real TLS termination exists; an operator who has confirmed every
subdomain of their chosen domain is HTTPS-only can add either at their own reverse proxy.

Verified empirically against the actual production artifact, not assumed: built
`apps/dashboard` with `output: "standalone"`, assembled the runtime tree exactly as
`apps/dashboard/Dockerfile` does (`.next/standalone` + `.next/static` + `public/`), started
`node apps/dashboard/server.js` with `NODE_ENV=production`, and confirmed via real HTTP requests
that every header above is present -- on a static page (`/`), a dynamic API route
(`/api/vigil/traces`, including on that route's `500` error response), and a static not-found page
(`/trace-not-found`, `404`) -- proving headers apply consistently across static/dynamic and
success/error responses, not only the happy path.

## Known limitations

- **CORS**: the deny-by-default policy assumes today's architecture (no browser-based cross-origin
  consumer). If one is introduced, an operator must explicitly configure
  `VIGIL_API_CORS_ALLOWED_ORIGINS` -- there is no automatic detection or opt-in path.
- **CSP `style-src 'unsafe-inline'`**: accepted, scoped, and documented above, but it is a real
  relaxation from a maximally strict CSP. A future nonce-based or hash-based approach for the two
  specific inline-style sources (next/font's injected `<style>` tag, Recharts' inline `style`
  attributes) was considered and rejected for this phase: `next/font`'s inline style injection is
  framework-internal and not designed to be nonce-compatible without wrapping/patching Next.js
  itself, and Recharts generates dynamic, per-render style values that can't be pre-computed as
  fixed hashes. Revisit if either library adds first-class CSP nonce support upstream.
- **HSTS is inert until TLS termination exists** -- see above; this is a deliberate, documented
  consequence of ADR 006's existing scope, not a new gap introduced here.
- Neither CORS nor the dashboard headers change anything about authentication, tenant isolation, or
  the rate limiting introduced in the prior Phase 4C commit -- both are additive, orthogonal
  hardening layers.

## Consequences

- A future browser-based API consumer requires an explicit `VIGIL_API_CORS_ALLOWED_ORIGINS`
  configuration change; forgetting it will surface as a browser-console CORS error, not a silent
  security gap (the default remains deny, never permissive-by-omission).
- Any future dashboard feature that needs a new external resource origin (an image CDN, an
  external font, a new API endpoint) requires updating the corresponding CSP directive in
  `next.config.ts` deliberately, or that resource will be blocked by the browser -- this is the
  intended trade-off of a real CSP over no CSP.
- Real TLS termination, whenever introduced per an operator's own infrastructure, requires no
  application-side change for HSTS to become active.
