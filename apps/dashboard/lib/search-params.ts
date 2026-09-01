import { DEFAULT_TIME_RANGE_PRESET, isTimeRangePreset, type TimeRangePreset } from "./time-range";

/**
 * URL <-> filter-state serialization for the Traces page, so every filter
 * combination (and the current pagination cursor) is bookmarkable/shareable
 * and survives a refresh. Kept as pure functions, independent of any
 * router/React APIs, so they're trivially unit-testable.
 */

/**
 * Page size for GET /v1/traces, shared by the Server Component initial
 * fetch (app/traces/page.tsx) and the Client Component that refetches on
 * interaction (components/traces/TracesExplorer.tsx). Deliberately lives
 * in this plain module, not in TracesExplorer.tsx itself: a "use client"
 * module's exports are only meaningful across the server/client boundary
 * as component references, not as plain values -- a Server Component
 * importing a constant from a "use client" file and using its value
 * directly (not passing it through as a prop) fails at request time.
 */
export const TRACE_LIST_PAGE_SIZE = 20;

export interface TraceFilters {
  range: TimeRangePreset;
  environment?: string;
  resource?: string;
  /** `undefined` = no filter (all traces), not `false`. */
  hasError?: boolean;
  cursor?: string;
}

export const DEFAULT_TRACE_FILTERS: TraceFilters = { range: DEFAULT_TIME_RANGE_PRESET };

export function traceFiltersToSearchParams(filters: TraceFilters): URLSearchParams {
  const params = new URLSearchParams();
  params.set("range", filters.range);
  if (filters.environment) params.set("environment", filters.environment);
  if (filters.resource) params.set("resource", filters.resource);
  if (filters.hasError !== undefined) params.set("has_error", String(filters.hasError));
  if (filters.cursor) params.set("cursor", filters.cursor);
  return params;
}

export function searchParamsToTraceFilters(
  params: URLSearchParams | Record<string, string | string[] | undefined>,
): TraceFilters {
  const get = (key: string): string | null => {
    if (params instanceof URLSearchParams) return params.get(key);
    const value = params[key];
    return Array.isArray(value) ? (value[0] ?? null) : (value ?? null);
  };

  const rangeParam = get("range");
  const hasErrorParam = get("has_error");

  return {
    range: isTimeRangePreset(rangeParam) ? rangeParam : DEFAULT_TIME_RANGE_PRESET,
    environment: get("environment") ?? undefined,
    resource: get("resource") ?? undefined,
    hasError: hasErrorParam === null ? undefined : hasErrorParam === "true",
    cursor: get("cursor") ?? undefined,
  };
}

/** Same filters, but with the pagination cursor cleared -- used whenever a
 * filter itself changes, since a stale cursor from a different filter
 * combination is meaningless. */
export function withoutCursor(filters: TraceFilters): TraceFilters {
  const rest = { ...filters };
  delete rest.cursor;
  return rest;
}
