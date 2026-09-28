# 9. Public Demo Architecture

- Status: Accepted
- Date: 2026-09-28

## Context

Vigil is primarily a GitHub portfolio project. The README's "Live Demo" link must lead to a public,
functional Vigil that a visitor can actually use, at ₹0 cost, with no credit card, no billing
account, and no dependency on the developer's PC being on.

The production architecture (ADR 006) cannot meet that bar: it needs an always-on host for
PostgreSQL, ClickHouse, a long-running `worker` and `poller`, and the in-process BGE model. Every
option that hosts it for free still requires a credit card, a billing account, or a VM that can be
reclaimed. This ADR does not change that architecture. It adds a separate, deliberately small demo
backend that serves the same `/v1` contract the dashboard already uses, at portfolio/demo scale,
not production SaaS scale.

The dashboard makes this possible without touching its code: it is a pure BFF. Every backend call
is server-side, through the single `VIGIL_API_BASE_URL` setting (`lib/api/http.ts`,
`lib/api/dashboardAuth.ts`, `lib/api/workspace.ts`), and the browser never calls the API directly
(`proxy.ts` sets `connect-src 'self'`; see ADR 007). Any backend that honors the same HTTP contract
is indistinguishable to it.

## Decision

### 1. Topology

```
GitHub README "Live Demo"
  -> existing Netlify Vigil dashboard (unchanged code)
  -> Cloudflare Worker demo API (apps/demo-api/)
  -> Cloudflare D1
```

The dashboard's Netlify environment sets `VIGIL_API_BASE_URL` and `VIGIL_PUBLIC_API_BASE_URL` to
the Worker's URL. That configuration is the dashboard's only change. The demo API is deployed from
GitHub, which remains the source of truth, as for production.

### 2. The demo API preserves the existing `/v1` contract

The demo API implements exactly the endpoints the dashboard calls, with request and response shapes
matching `apps/dashboard/lib/api/types.ts` and `workspaceTypes.ts`:

- Auth: `POST /v1/auth/signup`, `POST /v1/auth/login`, `GET /v1/auth/session`,
  `POST /v1/auth/logout`.
- Workspace: `GET /v1/me`, `POST /v1/organizations`, `POST /v1/organizations/{id}/projects`,
  `GET`/`POST /v1/projects/{id}/api-keys`, `POST /v1/projects/{id}/api-keys/{key_id}/revoke`.
- Ingestion and reads: `POST /v1/traces`, `GET /v1/traces`, `GET /v1/traces/{trace_id}`,
  `GET /v1/traces/{trace_id}/spans/{span_id}`.
- Evaluation: `GET`/`PUT /v1/evaluations/configs[/{evaluator_name}]`,
  `GET /v1/evaluations/jobs`, `GET /v1/traces/{trace_id}/spans/{span_id}/evaluations`.
- Analytics: `GET /v1/analytics/spans`, `GET /v1/analytics/llm-usage`.

Preserved exactly: the `X-Vigil-Session-Token`, `X-Vigil-Project-Id`, and `Authorization: Bearer`
headers; the `{detail: ...}` error body and its status codes (401, 404, 409, 422, 429); the
`vgl_<hex>.<urlsafe>` API-key shape; decimal costs serialized as strings; field truncation
(`*_size_bytes`, `*_truncated`); and the deterministic SHA-256 sampling rule (ADR 005). Internal
endpoints (`POST /v1/evaluations/jobs`, `POST /v1/provisioning/bootstrap`) are not implemented.

This supports the eight demo capabilities: sign up, log in, create a project, generate an API key,
send a test trace, view traces, run/view TF-IDF relevance evaluation, and view analytics.

### 3. Demo substitutions

- **D1 replaces PostgreSQL and ClickHouse for demo storage.** One SQLite schema holds the
  PostgreSQL tables the demo needs (`users`, `dashboard_sessions`, `organizations`,
  `organization_memberships`, `projects`, `api_keys`, `evaluator_configs`, `evaluation_jobs`) and
  the two ClickHouse tables (`spans`, `evaluation_results`), with ClickHouse `Map`/`Nested` columns
  stored as JSON text. `evaluation_poller_checkpoint` and `provisioning_bootstrap` are not needed.
- **Evaluation runs synchronously during trace ingestion**, replacing the production `poller` and
  `worker`. For each ingested `span_type = 'llm'` span whose evaluator is enabled and sampled, the
  demo API evaluates it within the same request and writes a `succeeded` `evaluation_jobs` row
  plus its `evaluation_results` row, so the existing jobs and results views work unchanged.
- **Relevance is a TypeScript port of the TF-IDF `relevance` evaluator** (ADR 004): the same
  tokenization, English stop-word list, smoothed IDF, L2 normalization, cosine score, threshold,
  and labels, verified against scores produced by `services/evaluator`.
- **Demo authentication uses a separately implemented, fast password hash** (WebCrypto
  PBKDF2-SHA256, iteration count fitted to the Workers free-plan CPU budget of 10 ms per request).
  Production's scrypt parameters do not fit that budget. Session tokens and API keys keep
  production's SHA-256-at-rest scheme.

### 4. Out of scope for the initial demo

The production BGE `relevance_embedding` evaluator is not part of the initial demo; enabling it in
the demo is rejected with an explicit error rather than silently accepted. A Workers AI
implementation (`@cf/baai/bge-small-en-v1.5`, the same base model) is an optional future phase; its
scores would be close to, but not identical to, production's quantized ONNX model.

### 5. Isolation from production

The demo backend lives entirely under `apps/demo-api/`. The production architecture remains
completely untouched: `apps/api`, `services/worker`, `services/evaluator`, `packages/sdk-python`,
`infrastructure`, `.github/workflows/cd.yml`, and the existing dashboard API/BFF code. No
production data, credential, or image is shared with the demo.

## Known limitations

- **Public free-tier quotas.** The Workers free plan (100,000 requests/day, 10 ms CPU/request),
  D1's daily read/write allowances, and Netlify's free credits are shared by every visitor. When a
  quota is exhausted, the demo fails until it resets; nothing is billed. Exhausting Netlify's
  credits pauses the dashboard site itself.
- **Per-IP rate limiting** on signup, login, and ingestion protects those quotas from a single
  visitor.
- **Per-project data caps** bound spans stored per project, keeping D1 usage and per-request CPU
  predictable.
- **Demo data retention and cleanup.** Telemetry and evaluation data are demo-only and deleted
  after a short retention window by a scheduled cleanup; inactive demo accounts may also be
  removed. Nothing in the demo is durable user data.
- **Demo authentication is not equivalent to production authentication.** Its password hash is
  weaker than production's scrypt; the demo tells visitors not to reuse a real password.
- **Analytics operate on capped demo data.** Percentiles are computed over each project's capped
  span set, not with ClickHouse's `quantile` functions, and are exact only at that scale.
- **Two implementations of one contract.** The demo API must be kept in step with the dashboard's
  types through contract tests; drift would surface as broken demo pages, not production issues.

## Consequences

- The README's "Live Demo" link can stay functional at ₹0, with no card, billing account, or PC.
- Production deployment (ADR 006) is unchanged and remains the real, full architecture; the demo is
  explicitly a demonstration of the product surface, not of the production runtime.
- Future contract changes to the dashboard-facing `/v1` API must be mirrored in `apps/demo-api/`,
  or the demo will diverge.
