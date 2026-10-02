// Every limit in one place. Values marked "apps/api" match production
// defaults (apps/api/app/config.py); values marked "demo" exist only to keep
// a public demo inside Cloudflare's free quotas (docs/decisions/009).

export interface Env {
  DB: D1Database;
  /**
   * Shared secret matching the dashboard's VIGIL_API_DASHBOARD_CLIENT_IP_TOKEN.
   * When it matches X-Vigil-Dashboard-Token, X-Vigil-Client-IP is trusted as
   * the end user's IP (the dashboard calls this API server-to-server).
   */
  DASHBOARD_CLIENT_IP_TOKEN?: string;
}

// apps/api
export const SESSION_TTL_HOURS = 12;
export const MAX_SPANS_PER_REQUEST = 1000;
export const MAX_INPUT_BYTES = 64 * 1024;
export const MAX_OUTPUT_BYTES = 64 * 1024;
export const MAX_TOTAL_SPAN_BYTES = 256 * 1024;
export const MAX_QUERY_WINDOW_DAYS = 7;
export const DEFAULT_QUERY_WINDOW_HOURS = 24;
export const MAX_SPANS_PER_TRACE_RESPONSE = 2000;
export const MAX_ANALYTICS_GROUPS = 50;
export const DEFAULT_SAMPLING_RATE = 0.1;
export const DEFAULT_MAX_RETRIES = 3;

// demo: request size (apps/api allows 10 MiB; CPU/D1 budgets here are far smaller)
export const MAX_REQUEST_BODY_BYTES = 256 * 1024;

// demo: per-project / per-account data caps
export const MAX_SPANS_PER_PROJECT = 2000;
export const MAX_ORGANIZATIONS_PER_USER = 3;
export const MAX_PROJECTS_PER_ORGANIZATION = 5;
export const MAX_API_KEYS_PER_PROJECT = 10;

// demo: synchronous evaluation budget per ingest request (Workers CPU limit)
export const MAX_EVALUATIONS_PER_REQUEST = 25;
export const MAX_EVALUATION_TEXT_BYTES_PER_REQUEST = 128 * 1024;

// demo: retention, enforced by the scheduled cleanup
export const TELEMETRY_RETENTION_DAYS = 7;
export const INACTIVE_ACCOUNT_RETENTION_DAYS = 30;

// demo: fixed-window rate limits (requests per window)
export interface RateLimitRule {
  name: string;
  limit: number;
  windowSeconds: number;
}
export const RATE_LIMITS = {
  signupPerIp: { name: "signup-ip", limit: 5, windowSeconds: 3600 },
  loginPerIp: { name: "login-ip", limit: 20, windowSeconds: 600 },
  loginPerAccount: { name: "login-account", limit: 10, windowSeconds: 600 },
  ingestPerIp: { name: "ingest-ip", limit: 120, windowSeconds: 60 },
  ingestPerKey: { name: "ingest-key", limit: 60, windowSeconds: 60 },
} satisfies Record<string, RateLimitRule>;
