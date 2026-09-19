import { EvaluationsView } from "@/components/evaluations/EvaluationsView";
import { listEvaluationJobs, listEvaluatorConfigs } from "@/lib/api/evaluations";
import {
  VigilApiError,
  type EvaluationJobListResponse,
  type EvaluatorConfigListResponse,
} from "@/lib/api/types";
import { EVALUATION_JOB_LIST_PAGE_SIZE, searchParamsToEvaluationJobFilters } from "@/lib/evaluationJobFilters";

interface FetchError {
  status: number;
  message: string;
}

function toFetchError(reason: unknown): FetchError {
  if (reason instanceof VigilApiError) return { status: reason.status, message: reason.message };
  return { status: 0, message: "Unable to load this data." };
}

function settledValue<T>(result: PromiseSettledResult<T>): T | null {
  return result.status === "fulfilled" ? result.value : null;
}

function settledError<T>(result: PromiseSettledResult<T>): FetchError | null {
  return result.status === "rejected" ? toFetchError(result.reason) : null;
}

/**
 * No inner Suspense boundary here, unlike app/page.tsx's OverviewPage: that
 * page's explicit boundary exists specifically to keep a *nested* route's
 * notFound() call producing a real HTTP 404 (see proxy.ts) -- a root-level
 * loading.tsx would otherwise wrap it in an implicit Suspense boundary that
 * breaks that. This page has no nested dynamic segment and calls
 * notFound() nowhere, so that hazard doesn't apply, and the route-level
 * app/evaluations/loading.tsx fallback alone is enough -- the same
 * plain-async-Server-Component shape app/traces/page.tsx already uses. An
 * earlier version of this page added its own inner Suspense on top of that
 * loading.tsx anyway, which produced a double fallback: loading.tsx's
 * heading-plus-skeleton, then a flash down to the inner fallback's
 * skeleton-only content (heading gone), then the real page (heading back).
 */
export default async function EvaluationsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const resolvedSearchParams = await searchParams;
  const tabParam = resolvedSearchParams.tab;
  const initialTab = tabParam === "jobs" ? "jobs" : "config";
  const jobFilters = searchParamsToEvaluationJobFilters(resolvedSearchParams);

  const [configsResult, jobsResult] = await Promise.allSettled([
    listEvaluatorConfigs(),
    listEvaluationJobs({
      status: jobFilters.status,
      evaluator_name: jobFilters.evaluatorName,
      cursor: jobFilters.cursor,
      limit: EVALUATION_JOB_LIST_PAGE_SIZE,
    }),
  ]);

  const configsResponse = settledValue<EvaluatorConfigListResponse>(configsResult);

  return (
    <EvaluationsView
      initialTab={initialTab}
      initialConfigs={configsResponse ? configsResponse.configs : null}
      initialConfigsError={settledError(configsResult)}
      initialJobFilters={jobFilters}
      initialJobs={settledValue<EvaluationJobListResponse>(jobsResult)}
      initialJobsError={settledError(jobsResult)}
    />
  );
}
