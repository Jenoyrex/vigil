import { buildQueryString, type QueryParams } from "./queryString";
import { extractDetailMessage, VigilApiError, type ApiErrorBody } from "./types";

/**
 * Optional request body for a mutating call, mirroring
 * `lib/api/vigilClient.ts`'s identical `VigilFetchInit` -- see that
 * module's docstring for the full rationale. Omitted entirely -> plain
 * `GET`, identical to every call site that predates this option.
 */
export interface VigilProxyFetchInit {
  method?: "PUT";
  body?: unknown;
}

/**
 * Same-origin fetch helper for Client Components. Calls this app's own
 * `/api/vigil/**` proxy routes only -- never the real Vigil API, and never
 * with an API key (the proxy route attaches that server-side; this module
 * has no access to it and imports nothing that does). Used for interactive
 * updates -- filter changes, pagination, analytics mode switches, config
 * saves -- after a Server Component's initial fetch has rendered the page.
 *
 * `init` is additive, exactly like `vigilFetch`'s identical parameter:
 * every pre-existing call site is untouched by this signature change.
 */
export async function fetchVigilProxy<T>(
  path: string,
  params?: QueryParams,
  init?: VigilProxyFetchInit,
): Promise<T> {
  const response = await fetch(`${path}${buildQueryString(params)}`, {
    method: init?.method,
    headers: init?.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
    cache: "no-store",
  });

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const body = (await response.json()) as ApiErrorBody;
      detail = extractDetailMessage(body.detail);
    } catch {
      // Non-JSON error body -- fall through to the generic message below.
    }
    throw new VigilApiError(response.status, detail ?? "The telemetry API returned an error.");
  }

  return (await response.json()) as T;
}
