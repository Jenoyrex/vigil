"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { SkeletonTable } from "@/components/ui/Skeleton";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import { VigilApiError, type EvaluationJobListResponse, type EvaluationJobStatus } from "@/lib/api/types";
import { canGoBack, currentCursor, INITIAL_CURSOR_STACK, popCursor, pushCursor, type CursorStack } from "@/lib/cursor-history";
import { titleForStatus } from "@/lib/errorMessages";
import {
  EVALUATION_JOB_LIST_PAGE_SIZE,
  EVALUATION_JOB_STATUSES,
  evaluationJobFiltersToSearchParams,
  type EvaluationJobFilters,
} from "@/lib/evaluationJobFilters";

import { EvaluationJobsTable } from "./EvaluationJobsTable";

interface FetchError {
  status: number;
  message: string;
}

function toFetchError(error: unknown): FetchError {
  if (error instanceof VigilApiError) return { status: error.status, message: error.message };
  return { status: 0, message: "Unable to load evaluation jobs. Please retry." };
}

/** Updates the URL bar for bookmarkability without triggering a Next.js
 * navigation/Server Component re-fetch -- same convention as
 * components/traces/TracesExplorer.tsx's identical `syncUrl`. */
function syncUrl(filters: EvaluationJobFilters, cursor: string | null): void {
  const params = evaluationJobFiltersToSearchParams({ ...filters, cursor: cursor ?? undefined });
  const query = params.toString();
  window.history.replaceState(null, "", query ? `/evaluations?tab=jobs&${query}` : "/evaluations?tab=jobs");
}

export function EvaluationJobsExplorer({
  initialFilters,
  initialData,
  initialError,
}: {
  initialFilters: EvaluationJobFilters;
  initialData: EvaluationJobListResponse | null;
  initialError: FetchError | null;
}) {
  const [filters, setFilters] = useState<EvaluationJobFilters>(initialFilters);
  const [cursorStack, setCursorStack] = useState<CursorStack>(
    initialFilters.cursor ? [null, initialFilters.cursor] : INITIAL_CURSOR_STACK,
  );
  const [data, setData] = useState<EvaluationJobListResponse | null>(initialData);
  const [status, setStatus] = useState<"idle" | "loading" | "error">(initialError ? "error" : "idle");
  const [error, setError] = useState<FetchError | null>(initialError);

  async function fetchPage(nextFilters: EvaluationJobFilters, cursor: string | null): Promise<void> {
    setStatus("loading");
    setError(null);
    try {
      const result = await fetchVigilProxy<EvaluationJobListResponse>("/api/vigil/evaluations/jobs", {
        status: nextFilters.status,
        evaluator_name: nextFilters.evaluatorName,
        cursor: cursor ?? undefined,
        limit: EVALUATION_JOB_LIST_PAGE_SIZE,
      });
      setData(result);
      setStatus("idle");
    } catch (err) {
      setStatus("error");
      setError(toFetchError(err));
    }
  }

  function applyFilters(patch: Partial<EvaluationJobFilters>): void {
    const nextFilters = { ...filters, ...patch };
    setFilters(nextFilters);
    setCursorStack(INITIAL_CURSOR_STACK);
    syncUrl(nextFilters, null);
    void fetchPage(nextFilters, null);
  }

  /**
   * Shared by the evaluator-name input's onBlur and onKeyDown(Enter): a
   * no-op when the trimmed value already matches the applied filter, so
   * that (a) blurring the field without having changed it never resets
   * pagination back to page 1, and (b) pressing Enter (which applies the
   * filter) followed by the blur that naturally follows never issues a
   * second, identical request.
   */
  function applyEvaluatorNameFilter(rawValue: string): void {
    const nextValue = rawValue.trim() || undefined;
    if (nextValue === filters.evaluatorName) return;
    applyFilters({ evaluatorName: nextValue });
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

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs font-medium text-muted" htmlFor="job-status-filter">
          Status
          <select
            id="job-status-filter"
            value={filters.status ?? ""}
            onChange={(event) =>
              applyFilters({ status: (event.target.value || undefined) as EvaluationJobStatus | undefined })
            }
            className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground"
          >
            <option value="">All</option>
            {EVALUATION_JOB_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted" htmlFor="job-evaluator-filter">
          Evaluator name
          <input
            id="job-evaluator-filter"
            type="text"
            placeholder="e.g. relevance"
            defaultValue={filters.evaluatorName ?? ""}
            onBlur={(event) => applyEvaluatorNameFilter(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                applyEvaluatorNameFilter(event.currentTarget.value);
              }
            }}
            className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground placeholder:text-muted"
          />
        </label>
      </div>

      {status === "loading" ? <SkeletonTable rows={8} /> : null}

      {status === "error" && error ? (
        <ErrorBanner
          title={titleForStatus(error.status)}
          message={error.message}
          onRetry={() => void fetchPage(filters, currentCursor(cursorStack))}
        />
      ) : null}

      {status === "idle" && data ? (
        data.jobs.length === 0 ? (
          <EmptyState
            title="No evaluation jobs found"
            description="No jobs matched this filter combination. Jobs appear once an enabled evaluator has sampled-in spans to evaluate."
          />
        ) : (
          <>
            <EvaluationJobsTable jobs={data.jobs} />
            <div className="flex items-center justify-between">
              <p className="text-xs text-muted">{data.jobs.length} jobs</p>
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
