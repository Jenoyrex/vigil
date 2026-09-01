/**
 * Shared by both the server-only Vigil client (lib/api/vigilClient.ts) and
 * the client-safe proxy fetcher (lib/api/browserClient.ts) -- plain query
 * string construction, not API business logic, so sharing it isn't
 * duplicating anything the backend is responsible for. Has no
 * `import "server-only"` guard, so it's safe to import from Client
 * Components too.
 */

export type QueryParams = Record<string, string | number | boolean | undefined>;

export function buildQueryString(params?: QueryParams): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}
