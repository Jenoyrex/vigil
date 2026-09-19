import type { EvaluationJobStatus } from "./api/types";

/**
 * URL <-> filter-state serialization for the Evaluations page's Jobs tab,
 * mirroring lib/search-params.ts's TraceFilters pattern exactly (pure
 * functions, independent of any router/React API, trivially unit-testable).
 * Kept as its own module rather than folded into search-params.ts, which is
 * scoped to the Traces page's own filter shape.
 */

/** Page size for GET /v1/evaluations/jobs -- same reasoning as
 * lib/search-params.ts's TRACE_LIST_PAGE_SIZE for why this lives in a plain
 * module, not inside a "use client" component. */
export const EVALUATION_JOB_LIST_PAGE_SIZE = 20;

export const EVALUATION_JOB_STATUSES: readonly EvaluationJobStatus[] = [
  "pending",
  "running",
  "succeeded",
  "failed",
  "dead_letter",
];

function isEvaluationJobStatus(value: string | null): value is EvaluationJobStatus {
  return value !== null && (EVALUATION_JOB_STATUSES as readonly string[]).includes(value);
}

export interface EvaluationJobFilters {
  status?: EvaluationJobStatus;
  evaluatorName?: string;
  cursor?: string;
}

export const DEFAULT_EVALUATION_JOB_FILTERS: EvaluationJobFilters = {};

export function evaluationJobFiltersToSearchParams(filters: EvaluationJobFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.evaluatorName) params.set("evaluator_name", filters.evaluatorName);
  if (filters.cursor) params.set("cursor", filters.cursor);
  return params;
}

export function searchParamsToEvaluationJobFilters(
  params: URLSearchParams | Record<string, string | string[] | undefined>,
): EvaluationJobFilters {
  const get = (key: string): string | null => {
    if (params instanceof URLSearchParams) return params.get(key);
    const value = params[key];
    return Array.isArray(value) ? (value[0] ?? null) : (value ?? null);
  };

  const statusParam = get("status");

  return {
    status: isEvaluationJobStatus(statusParam) ? statusParam : undefined,
    evaluatorName: get("evaluator_name") ?? undefined,
    cursor: get("cursor") ?? undefined,
  };
}

/** Same filters, but with the pagination cursor cleared -- used whenever a
 * filter itself changes, since a stale cursor from a different filter
 * combination is meaningless (mirrors lib/search-params.ts's withoutCursor). */
export function withoutCursor(filters: EvaluationJobFilters): EvaluationJobFilters {
  const rest = { ...filters };
  delete rest.cursor;
  return rest;
}
