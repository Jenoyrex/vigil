/**
 * Maps an HTTP status (from the Vigil API, via the BFF proxy) to a short,
 * safe title. A plain module -- no "use client" -- so Server Components can
 * call it directly: a function exported from a "use client" module is only
 * usable across the server/client boundary as a component reference (to be
 * rendered), not invoked directly for its return value. Calling it from a
 * Server Component throws at request time instead. (See
 * lib/search-params.ts's TRACE_LIST_PAGE_SIZE for the same lesson applied
 * to a plain constant, and app/traces/[traceId]/page.tsx for where this was
 * first caught: calling the version of this function that used to live in
 * components/ui/ErrorBanner.tsx, a "use client" file.)
 */
export function titleForStatus(status: number | undefined): string {
  switch (status) {
    case 401:
      return "Authentication failed";
    case 404:
      return "Not found";
    case 422:
      return "Invalid request";
    case 503:
      return "Telemetry storage unavailable";
    default:
      return "Something went wrong";
  }
}
