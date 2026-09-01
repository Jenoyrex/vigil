/** `?start=<ISO timestamp>` (set by TraceTable's link) -> a `YYYY-MM-DD`
 * partition-pruning hint for GET /v1/traces/{trace_id}. Shared by the
 * trace-detail page and proxy.ts's pre-render existence check, so both
 * derive the same start_date from the same query param. */
export function deriveStartDate(startParam: string | undefined): string | undefined {
  if (!startParam) return undefined;
  const date = new Date(startParam);
  if (Number.isNaN(date.getTime())) return undefined;
  return date.toISOString().slice(0, 10);
}
