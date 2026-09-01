import { Suspense } from "react";

import { OverviewView } from "@/components/overview/OverviewView";
import { SkeletonStatTiles, SkeletonTable } from "@/components/ui/Skeleton";
import { getLlmUsageAnalytics, getSpanAnalytics } from "@/lib/api/analytics";
import { listTraces } from "@/lib/api/traces";
import { VigilApiError, type LlmUsageResponse, type SpanAnalyticsResponse, type TraceListResponse } from "@/lib/api/types";
import { bucketForPreset, DEFAULT_TIME_RANGE_PRESET, resolveTimeRange } from "@/lib/time-range";

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
 * An explicit Suspense boundary here (rather than a file-convention
 * app/loading.tsx) deliberately keeps this page's loading fallback local
 * to "/": a root-level loading.tsx wraps every nested route lacking its
 * own more specific one in an implicit Suspense boundary, which is
 * exactly the mechanism that stops a nested route's notFound() from ever
 * producing a real HTTP 404 status (see proxy.ts and
 * app/trace-not-found/page.tsx). The fallback UI is unchanged from the
 * former app/loading.tsx.
 */
export default function OverviewPage() {
  return (
    <Suspense
      fallback={
        <div className="space-y-6">
          <SkeletonStatTiles />
          <SkeletonTable rows={6} />
        </div>
      }
    >
      <OverviewData />
    </Suspense>
  );
}

async function OverviewData() {
  const range = DEFAULT_TIME_RANGE_PRESET;
  const window = resolveTimeRange(range);
  const bucket = bucketForPreset(range);

  const [spanAnalyticsResult, llmUsageResult, trendResult, recentTracesResult] = await Promise.allSettled([
    getSpanAnalytics({ start_time_from: window.start_time_from, start_time_to: window.start_time_to }),
    getLlmUsageAnalytics({ start_time_from: window.start_time_from, start_time_to: window.start_time_to }),
    getSpanAnalytics({
      start_time_from: window.start_time_from,
      start_time_to: window.start_time_to,
      bucket,
    }),
    listTraces({ start_time_from: window.start_time_from, start_time_to: window.start_time_to, limit: 8 }),
  ]);

  return (
    <OverviewView
      initialRange={range}
      initialData={{
        spanAnalytics: settledValue<SpanAnalyticsResponse>(spanAnalyticsResult),
        llmUsage: settledValue<LlmUsageResponse>(llmUsageResult),
        trend: settledValue<SpanAnalyticsResponse>(trendResult),
        recentTraces: settledValue<TraceListResponse>(recentTracesResult),
      }}
      initialErrors={{
        spanAnalytics: settledError(spanAnalyticsResult),
        llmUsage: settledError(llmUsageResult),
        trend: settledError(trendResult),
        recentTraces: settledError(recentTracesResult),
      }}
    />
  );
}
