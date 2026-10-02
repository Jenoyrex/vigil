// Demo-only fixed-window rate limiting stored in D1 (the Workers free plan
// has no shared in-memory state). One upsert per check.

import type { Env, RateLimitRule } from "./config";
import { ApiError } from "./http";
import { constantTimeStringEqual } from "./security";

export async function enforceRateLimit(env: Env, rule: RateLimitRule, subject: string, now = Date.now()): Promise<void> {
  const windowMs = rule.windowSeconds * 1000;
  const windowStart = Math.floor(now / windowMs) * windowMs;
  const row = await env.DB.prepare(
    `INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1)
     ON CONFLICT (key) DO UPDATE SET
       count = CASE WHEN rate_limits.window_start = excluded.window_start THEN rate_limits.count + 1 ELSE 1 END,
       window_start = excluded.window_start
     RETURNING count`,
  )
    .bind(`${rule.name}:${subject}`, windowStart)
    .first<{ count: number }>();
  if ((row?.count ?? 0) > rule.limit) {
    const retryAfter = Math.max(1, Math.ceil((windowStart + windowMs - now) / 1000));
    throw new ApiError(429, `Rate limit exceeded. Retry after ${retryAfter} seconds.`, {
      "Retry-After": String(retryAfter),
    });
  }
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const IPV6 = /^[0-9a-fA-F:.]{2,45}$/;

/**
 * The end user's IP. The dashboard calls this API from its own server, so the
 * connecting IP is the dashboard's; apps/api's attribution contract
 * (X-Vigil-Dashboard-Token + X-Vigil-Client-IP) is honored the same way.
 */
export function clientIp(request: Request, env: Env): string {
  const peer = request.headers.get("cf-connecting-ip") ?? "unknown";
  const expected = env.DASHBOARD_CLIENT_IP_TOKEN;
  const token = request.headers.get("x-vigil-dashboard-token");
  const forwarded = request.headers.get("x-vigil-client-ip")?.trim();
  if (!expected || token === null || !forwarded) return peer;
  if (!constantTimeStringEqual(token, expected)) return peer;
  return IPV4.test(forwarded) || (forwarded.includes(":") && IPV6.test(forwarded)) ? forwarded.toLowerCase() : peer;
}
