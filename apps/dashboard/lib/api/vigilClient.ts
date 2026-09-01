import "server-only";

import { buildQueryString, type QueryParams } from "./queryString";
import { extractDetailMessage, VigilApiError, type ApiErrorBody } from "./types";

/**
 * The only place the Vigil API key ever exists in this application.
 *
 * `import "server-only"` (above) makes the Next.js build fail if this
 * module is ever imported, even transitively, by a Client Component --
 * that's its entire purpose, and is why this module holds the key rather
 * than each caller reading `process.env` directly. Both env vars are
 * server-only by convention (no `NEXT_PUBLIC_` prefix), so even a build
 * misconfiguration can't leak them into client-bundled code.
 *
 * Callers are: Server Components (app/**\/page.tsx, fetching directly for
 * the initial render) and the app/api/vigil/** route handlers (the
 * same-origin BFF proxy Client Components fetch from for interactive
 * filter/pagination changes). Nothing else should import this file.
 */

function requireEnv(name: "VIGIL_API_BASE_URL" | "VIGIL_API_KEY"): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not configured. Set it as a server-side environment variable ` +
        `(never NEXT_PUBLIC_${name}) before starting the dashboard.`,
    );
  }
  return value;
}

export type { QueryParams };

/**
 * Fetch one path from the real Vigil API, attaching the server-side API
 * key. Never called from client code -- see the module docstring above.
 *
 * Deliberately does not log the request URL's query string, the response
 * body, or any header: query params can contain user-supplied filter text,
 * and response bodies are telemetry content (see "Avoid logging telemetry
 * payloads or credentials" in the BFF proxy requirements). On failure, only
 * the path and status code are logged.
 */
export async function vigilFetch<T>(path: string, params?: QueryParams): Promise<T> {
  const baseUrl = requireEnv("VIGIL_API_BASE_URL");
  const apiKey = requireEnv("VIGIL_API_KEY");

  const url = `${baseUrl.replace(/\/+$/, "")}${path}${buildQueryString(params)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      headers: { Authorization: `Bearer ${apiKey}` },
      // Telemetry data changes continuously; never serve a stale cached
      // response for a dashboard view.
      cache: "no-store",
    });
  } catch {
    // Network-level failure (DNS, connection refused, timeout). Never
    // include the underlying error, which could echo the target URL/host.
    console.error(`vigil api: network error calling ${path}`);
    throw new VigilApiError(503, "Unable to reach the telemetry API. Please retry.");
  }

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const body = (await response.json()) as ApiErrorBody;
      detail = extractDetailMessage(body.detail);
    } catch {
      // Non-JSON error body -- fall through to the generic message below.
    }
    console.error(`vigil api: ${path} responded ${response.status}`);
    throw new VigilApiError(response.status, detail ?? "The telemetry API returned an error.");
  }

  return (await response.json()) as T;
}
