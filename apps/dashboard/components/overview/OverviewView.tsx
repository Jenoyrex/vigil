"use client";

import { useMemo, useState } from "react";

import { TimeSeriesChart } from "@/components/analytics/TimeSeriesChart";
import { TimeRangePicker } from "@/components/layout/TimeRangePicker";
import { TraceTable } from "@/components/traces/TraceTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { FilterDropdown } from "@/components/ui/FilterDropdown";
import { SkeletonStatTiles, SkeletonTable } from "@/components/ui/Skeleton";
import { StatTile } from "@/components/ui/StatTile";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import { VigilApiError, type LlmUsageResponse, type SpanAnalyticsResponse, type TraceListResponse } from "@/lib/api/types";
import { titleForStatus } from "@/lib/errorMessages";
import { useDimensionOptions } from "@/lib/hooks/useDimensionOptions";
import { formatCost, formatCount, formatDuration, formatPercent } from "@/lib/format";
import { bucketForPreset, resolveTimeRange, type TimeRangePreset } from "@/lib/time-range";

interface FetchError {
  status: number;
  message: string;
}

interface OverviewData {
  spanAnalytics: SpanAnalyticsResponse | null;
  llmUsage: LlmUsageResponse | null;
  trend: SpanAnalyticsResponse | null;
  recentTraces: TraceListResponse | null;
}

interface OverviewErrors {
  spanAnalytics: FetchError | null;
  llmUsage: FetchError | null;
  trend: FetchError | null;
  recentTraces: FetchError | null;
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

export function OverviewView({
  initialRange,
  initialData,
  initialErrors,
}: {
  initialRange: TimeRangePreset;
  initialData: OverviewData;
  initialErrors: OverviewErrors;
}) {
  const [range, setRange] = useState<TimeRangePreset>(initialRange);
  const [environment, setEnvironment] = useState<string | undefined>(undefined);
  const [data, setData] = useState<OverviewData>(initialData);
  const [errors, setErrors] = useState<OverviewErrors>(initialErrors);
  const [loading, setLoading] = useState(false);

  // Memoized on `range` alone -- see LlmUsagePanel.tsx for why an
  // unmemoized call here would cause an infinite fetch loop (via
  // useDimensionOptions' effect depending on timeRange.start_time_*).
  const timeRange = useMemo(() => resolveTimeRange(range), [range]);
  const environmentOptions = useDimensionOptions("environment", timeRange);
  const bucket = bucketForPreset(range);

  async function refetch(nextRange: TimeRangePreset, nextEnvironment: string | undefined): Promise<void> {
    setLoading(true);
    const window = resolveTimeRange(nextRange);
    const nextBucket = bucketForPreset(nextRange);

    const [spanAnalyticsResult, llmUsageResult, trendResult, recentTracesResult] = await Promise.allSettled([
      fetchVigilProxy<SpanAnalyticsResponse>("/api/vigil/analytics/spans", {
        start_time_from: window.start_time_from,
        start_time_to: window.start_time_to,
        environment: nextEnvironment,
      }),
      fetchVigilProxy<LlmUsageResponse>("/api/vigil/analytics/llm-usage", {
        start_time_from: window.start_time_from,
        start_time_to: window.start_time_to,
        environment: nextEnvironment,
      }),
      fetchVigilProxy<SpanAnalyticsResponse>("/api/vigil/analytics/spans", {
        start_time_from: window.start_time_from,
        start_time_to: window.start_time_to,
        environment: nextEnvironment,
        bucket: nextBucket,
      }),
      fetchVigilProxy<TraceListResponse>("/api/vigil/traces", {
        start_time_from: window.start_time_from,
        start_time_to: window.start_time_to,
        environment: nextEnvironment,
        limit: 8,
      }),
    ]);

    setData({
      spanAnalytics: settledValue(spanAnalyticsResult),
      llmUsage: settledValue(llmUsageResult),
      trend: settledValue(trendResult),
      recentTraces: settledValue(recentTracesResult),
    });
    setErrors({
      spanAnalytics: settledError(spanAnalyticsResult),
      llmUsage: settledError(llmUsageResult),
      trend: settledError(trendResult),
      recentTraces: settledError(recentTracesResult),
    });
    setLoading(false);
  }

  function handleRangeChange(nextRange: TimeRangePreset): void {
    setRange(nextRange);
    void refetch(nextRange, environment);
  }

  function handleEnvironmentChange(nextEnvironment: string | undefined): void {
    setEnvironment(nextEnvironment);
    void refetch(range, nextEnvironment);
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-lg font-semibold text-foreground">Overview</h1>
        <div className="flex flex-wrap items-end gap-3">
          <FilterDropdown
            label="Environment"
            value={environment}
            options={environmentOptions.map((value) => ({ value, label: value }))}
            onChange={handleEnvironmentChange}
          />
          <TimeRangePicker value={range} onChange={handleRangeChange} />
        </div>
      </div>

      {loading ? <SkeletonStatTiles count={7} /> : null}

      {!loading && errors.spanAnalytics ? (
        <ErrorBanner title={titleForStatus(errors.spanAnalytics.status)} message={errors.spanAnalytics.message} />
      ) : null}

      {!loading && data.spanAnalytics ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-7">
          <StatTile label="Total Spans" value={formatCount(data.spanAnalytics.span_count ?? 0)} />
          <StatTile
            label="Error Rate"
            value={formatPercent(data.spanAnalytics.error_rate ?? 0)}
            tone={(data.spanAnalytics.error_span_count ?? 0) > 0 ? "error" : "default"}
            sublabel={`${formatCount(data.spanAnalytics.error_span_count ?? 0)} errors`}
          />
          <StatTile label="p50 Latency" value={formatDuration(data.spanAnalytics.latency_ms?.p50 ?? 0)} />
          <StatTile label="p90 Latency" value={formatDuration(data.spanAnalytics.latency_ms?.p90 ?? 0)} />
          <StatTile label="p99 Latency" value={formatDuration(data.spanAnalytics.latency_ms?.p99 ?? 0)} />
          {!errors.llmUsage && data.llmUsage ? (
            <>
              <StatTile label="LLM Cost" value={formatCost(data.llmUsage.total_cost_usd ?? null)} />
              <StatTile label="Total Tokens" value={formatCount(data.llmUsage.total_tokens ?? 0)} />
            </>
          ) : null}
        </div>
      ) : null}

      {!loading && !data.spanAnalytics && !errors.spanAnalytics ? (
        <EmptyState title="No telemetry received yet" description="Send spans with the Vigil SDK to see statistics here." />
      ) : null}

      {!loading && errors.trend ? null : !loading && data.trend?.buckets ? (
        <div className="rounded-lg border border-border p-4">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Request volume</p>
          <TimeSeriesChart buckets={data.trend.buckets} bucket={bucket} />
        </div>
      ) : null}

      <div className="space-y-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted">Recent traces</p>
        {loading ? <SkeletonTable rows={5} /> : null}
        {!loading && errors.recentTraces ? (
          <ErrorBanner title={titleForStatus(errors.recentTraces.status)} message={errors.recentTraces.message} />
        ) : null}
        {!loading && data.recentTraces ? (
          data.recentTraces.traces.length === 0 ? (
            <EmptyState title="No recent traces" description="No traces matched this time range and filter." />
          ) : (
            <TraceTable traces={data.recentTraces.traces} />
          )
        ) : null}
      </div>
    </div>
  );
}
