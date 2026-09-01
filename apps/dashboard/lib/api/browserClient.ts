import { buildQueryString, type QueryParams } from "./queryString";
import { extractDetailMessage, VigilApiError, type ApiErrorBody } from "./types";

/**
 * Same-origin fetch helper for Client Components. Calls this app's own
 * `/api/vigil/**` proxy routes only -- never the real Vigil API, and never
 * with an API key (the proxy route attaches that server-side; this module
 * has no access to it and imports nothing that does). Used for interactive
 * updates -- filter changes, pagination, analytics mode switches -- after a
 * Server Component's initial fetch has rendered the page.
 */
export async function fetchVigilProxy<T>(path: string, params?: QueryParams): Promise<T> {
  const response = await fetch(`${path}${buildQueryString(params)}`, { cache: "no-store" });

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
