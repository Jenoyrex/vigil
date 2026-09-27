import "server-only";

import { buildQueryString, type QueryParams } from "./queryString";
import { extractDetailMessage, VigilApiError, type ApiErrorBody } from "./types";

/**
 * The one low-level HTTP call from this app's server to apps/api.
 *
 * `import "server-only"` makes the build fail if this module is ever
 * imported, even transitively, by a Client Component: the credentials it
 * attaches (a dashboard session token, or a customer API key for the
 * onboarding test trace) must never reach browser code.
 *
 * Callers pick the credentials explicitly -- see lib/api/vigilClient.ts
 * (project-scoped reads/config, session + project id) and
 * lib/api/workspace.ts (session-only workspace routes).
 */

export type { QueryParams };

export const SESSION_TOKEN_HEADER = "X-Vigil-Session-Token";
export const PROJECT_ID_HEADER = "X-Vigil-Project-Id";

export interface ApiFetchInit {
  method?: "GET" | "POST" | "PUT";
  body?: unknown;
  headers?: Record<string, string>;
}

function requireApiBaseUrl(): string {
  const value = process.env.VIGIL_API_BASE_URL;
  if (!value) {
    throw new Error(
      "VIGIL_API_BASE_URL is not configured. Set it as a server-side environment variable " +
        "(never NEXT_PUBLIC_VIGIL_API_BASE_URL) before starting the dashboard.",
    );
  }
  return value;
}

/**
 * Deliberately never logs the query string, a request/response body or any
 * header: query params can carry user-supplied filter text, bodies are
 * telemetry or configuration content, and headers carry credentials. On
 * failure only the path and status code are logged.
 */
export async function apiFetch<T>(
  path: string,
  params?: QueryParams,
  init?: ApiFetchInit,
): Promise<T> {
  const url = `${requireApiBaseUrl().replace(/\/+$/, "")}${path}${buildQueryString(params)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method: init?.method,
      headers: {
        ...init?.headers,
        ...(init?.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
      // Telemetry/configuration data changes continuously; never serve a
      // stale cached response, and never cache a mutating request.
      cache: "no-store",
    });
  } catch {
    // Network-level failure (DNS, connection refused, timeout). Never
    // include the underlying error, which could echo the target URL/host.
    console.error(`vigil api: network error calling ${path}`);
    throw new VigilApiError(503, "Unable to reach the Vigil API. Please retry.");
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
    throw new VigilApiError(response.status, detail ?? "The Vigil API returned an error.");
  }

  return (await response.json()) as T;
}
