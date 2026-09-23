import { isIP } from "node:net";

/**
 * How many reverse proxies the operator has declared trusted in front of
 * this dashboard (`VIGIL_DASHBOARD_TRUSTED_PROXY_HOPS`). Anything that is
 * not a plain non-negative integer -- unset, empty, negative, fractional,
 * garbage -- is treated as 0, i.e. "no trusted proxy", the safe answer.
 */
export function parseTrustedProxyHops(raw: string | undefined): number {
  if (raw === undefined || !/^\d{1,3}$/.test(raw.trim())) return 0;
  return Number.parseInt(raw.trim(), 10);
}

/**
 * The end-user IP for login rate limiting, or null when it cannot be
 * determined safely.
 *
 * Why this is not simply "read X-Forwarded-For": Next.js only fills that
 * header from the socket when the request did not already carry one
 * (`req.headers['x-forwarded-for'] ??= socket.remoteAddress`), and route
 * handlers have no access to the socket. So on a directly exposed dashboard
 * the header is whatever the browser sent, and trusting it would let anyone
 * pick their own rate-limit identity. It is believed only when the operator
 * has declared `trustedHops` >= 1 reverse proxies: each trusted proxy
 * appends the address it saw connecting to it, so the entry `trustedHops`
 * positions from the right is the address seen by the outermost trusted
 * proxy -- everything to its left is client-controlled and ignored. With
 * `trustedHops` = 0 (the default) this returns null and the API falls back
 * to its own direct-peer keying.
 */
export function deriveClientIp(xForwardedFor: string | null, trustedHops: number): string | null {
  if (!xForwardedFor || trustedHops < 1) return null;

  const entries = xForwardedFor.split(",").map((entry) => entry.trim());
  const candidate = entries[entries.length - trustedHops];
  if (candidate === undefined || isIP(candidate) === 0) return null;
  return candidate;
}
