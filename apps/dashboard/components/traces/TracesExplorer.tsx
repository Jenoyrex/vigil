"use client";

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import type { FilterDropdownOption } from "@/components/ui/FilterDropdown";
import { SkeletonTable } from "@/components/ui/Skeleton";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import { VigilApiError, type TraceListResponse } from "@/lib/api/types";
import { canGoBack, currentCursor, INITIAL_CURSOR_STACK, popCursor, pushCursor, type CursorStack } from "@/lib/cursor-history";
import { titleForStatus } from "@/lib/errorMessages";
import { useDimensionOptions } from "@/lib/hooks/useDimensionOptions";
import {
  traceFiltersToSearchParams,
  TRACE_LIST_PAGE_SIZE,
  type TraceFilters as TraceFilterState,
} from "@/lib/search-params";
import { resolveTimeRange } from "@/lib/time-range";

import { TraceFilters, type ErrorFilter } from "./TraceFilters";
import { TraceTable } from "./TraceTable";

interface FetchError {
  status: number;
  message: string;
}

function toFetchError(error: unknown): FetchError {
  if (error instanceof VigilApiError) return { status: error.status, message: error.message };
  return { status: 0, message: "Unable to load traces. Please retry." };
}

function errorFilterFromHasError(hasError: boolean | undefined): ErrorFilter {
  if (hasError === undefined) return "all";
  return hasError ? "error" : "ok";
}

function hasErrorFromErrorFilter(value: ErrorFilter): boolean | undefined {
  if (value === "all") return undefined;
  return value === "error";
}

/** Updates the URL bar for bookmarkability without triggering a Next.js
 * navigation/Server Component re-fetch -- this component already has the
 * data it needs client-side. */
function syncUrl(filters: TraceFilterState, cursor: string | null): void {
  const params = traceFiltersToSearchParams({ ...filters, cursor: cursor ?? undefined });
  window.history.replaceState(null, "", `/traces?${params.toString()}`);
}

export function TracesExplorer({
  initialFilters,
  initialData,
  initialError,
}: {
  initialFilters: TraceFilterState;
  initialData: TraceListResponse | null;
  initialError: FetchError | null;
}) {
  const [filters, setFilters] = useState<TraceFilterState>(initialFilters);
  const [cursorStack, setCursorStack] = useState<CursorStack>(
    initialFilters.cursor ? [null, initialFilters.cursor] : INITIAL_CURSOR_STACK,
  );
  const [data, setData] = useState<TraceListResponse | null>(initialData);
  const [status, setStatus] = useState<"idle" | "loading" | "error">(initialError ? "error" : "idle");
  const [error, setError] = useState<FetchError | null>(initialError);

  // Memoized on filters.range alone -- see
  // components/analytics/LlmUsagePanel.tsx for why an unmemoized call here
  // would cause an infinite fetch loop.
  const timeRange = useMemo(() => resolveTimeRange(filters.range), [filters.range]);
  const environmentOptions = useDimensionOptions("environment", timeRange);
  const resourceOptions = useDimensionOptions("resource", timeRange);

  async function fetchPage(nextFilters: TraceFilterState, cursor: string | null): Promise<void> {
    setStatus("loading");
    setError(null);
    const range = resolveTimeRange(nextFilters.range);
    try {
      const result = await fetchVigilProxy<TraceListResponse>("/api/vigil/traces", {
        start_time_from: range.start_time_from,
        start_time_to: range.start_time_to,
        environment: nextFilters.environment,
        resource: nextFilters.resource,
        has_error: nextFilters.hasError,
        cursor: cursor ?? undefined,
        limit: TRACE_LIST_PAGE_SIZE,
      });
      setData(result);
      setStatus("idle");
    } catch (err) {
      setStatus("error");
      setError(toFetchError(err));
    }
  }

  function applyFilters(patch: Partial<TraceFilterState>): void {
    const nextFilters = { ...filters, ...patch };
    setFilters(nextFilters);
    setCursorStack(INITIAL_CURSOR_STACK);
    syncUrl(nextFilters, null);
    void fetchPage(nextFilters, null);
  }

  function handleNext(): void {
    const nextCursor = data?.next_cursor;
    if (!nextCursor) return;
    const nextStack = pushCursor(cursorStack, nextCursor);
    setCursorStack(nextStack);
    syncUrl(filters, nextCursor);
    void fetchPage(filters, nextCursor);
  }

  function handlePrevious(): void {
    const nextStack = popCursor(cursorStack);
    setCursorStack(nextStack);
    const cursor = currentCursor(nextStack);
    syncUrl(filters, cursor);
    void fetchPage(filters, cursor);
  }

  function toOptionList(values: string[]): FilterDropdownOption[] {
    return values.map((value) => ({ value, label: value }));
  }

  return (
    <div className="space-y-4">
      <TraceFilters
        range={filters.range}
        onRangeChange={(range) => applyFilters({ range })}
        environment={filters.environment}
        onEnvironmentChange={(environment) => applyFilters({ environment })}
        environmentOptions={toOptionList(environmentOptions)}
        resource={filters.resource}
        onResourceChange={(resource) => applyFilters({ resource })}
        resourceOptions={toOptionList(resourceOptions)}
        errorFilter={errorFilterFromHasError(filters.hasError)}
        onErrorFilterChange={(value) => applyFilters({ hasError: hasErrorFromErrorFilter(value) })}
      />

      {status === "loading" ? <SkeletonTable rows={8} /> : null}

      {status === "error" && error ? (
        <ErrorBanner
          title={titleForStatus(error.status)}
          message={error.message}
          onRetry={() => void fetchPage(filters, currentCursor(cursorStack))}
        />
      ) : null}

      {status === "idle" && data ? (
        data.traces.length === 0 ? (
          <EmptyState
            title="No traces found"
            description="No traces matched this time range and filter combination."
            action={
              <Button variant="secondary" onClick={() => applyFilters({ range: "7d" })}>
                Widen to last 7 days
              </Button>
            }
          />
        ) : (
          <>
            <TraceTable traces={data.traces} />
            <div className="flex items-center justify-between">
              <p className="text-xs text-muted">{data.traces.length} traces</p>
              <div className="flex gap-2">
                <Button variant="secondary" onClick={handlePrevious} disabled={!canGoBack(cursorStack)}>
                  Previous
                </Button>
                <Button variant="secondary" onClick={handleNext} disabled={!data.next_cursor}>
                  Next
                </Button>
              </div>
            </div>
          </>
        )
      ) : null}
    </div>
  );
}
