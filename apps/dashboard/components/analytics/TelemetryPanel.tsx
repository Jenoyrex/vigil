"use client";

import { useEffect, useMemo, useState } from "react";

import { TimeRangePicker } from "@/components/layout/TimeRangePicker";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { FilterDropdown } from "@/components/ui/FilterDropdown";
import { SkeletonStatTiles } from "@/components/ui/Skeleton";
import { StatTile } from "@/components/ui/StatTile";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import { VigilApiError, type SpanAnalyticsResponse, type SpanBucket, type SpanGroupBy } from "@/lib/api/types";
import { titleForStatus } from "@/lib/errorMessages";
import { useDimensionOptions } from "@/lib/hooks/useDimensionOptions";
import { formatCount, formatDuration, formatPercent } from "@/lib/format";
import { bucketForPreset, resolveTimeRange, type TimeRangePreset } from "@/lib/time-range";

import { ModeSelector } from "./ModeSelector";
import { SpanGroupedTable } from "./SpanGroupedTable";
import { TimeSeriesChart } from "./TimeSeriesChart";

type Mode = "totals" | "grouped" | "timeseries";

const GROUP_BY_OPTIONS: { value: SpanGroupBy; label: string }[] = [
  { value: "environment", label: "Environment" },
  { value: "span_type", label: "Span type" },
  { value: "release", label: "Release" },
  { value: "resource", label: "Resource" },
];

interface FetchError {
  status: number;
  message: string;
}

export function TelemetryPanel() {
  const [range, setRange] = useState<TimeRangePreset>("24h");
  const [environment, setEnvironment] = useState<string | undefined>(undefined);
  const [resource, setResource] = useState<string | undefined>(undefined);
  const [spanType, setSpanType] = useState<string | undefined>(undefined);
  const [mode, setMode] = useState<Mode>("totals");
  const [groupBy, setGroupBy] = useState<SpanGroupBy>("environment");

  const [data, setData] = useState<SpanAnalyticsResponse | null>(null);
  const [status, setStatus] = useState<"loading" | "idle" | "error">("loading");
  const [error, setError] = useState<FetchError | null>(null);

  // Memoized on `range` alone -- see LlmUsagePanel.tsx for why an
  // unmemoized call here would cause an infinite fetch loop.
  const timeRange = useMemo(() => resolveTimeRange(range), [range]);
  const bucket: SpanBucket = bucketForPreset(range);
  const environmentOptions = useDimensionOptions("environment", timeRange);
  const resourceOptions = useDimensionOptions("resource", timeRange);
  const spanTypeOptions = useDimensionOptions("span_type", timeRange);

  useEffect(() => {
    let cancelled = false;
    // Runs on mount and on every filter/mode change -- there is no user
    // event to hang the "start loading" state update on instead.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStatus("loading");
    setError(null);

    fetchVigilProxy<SpanAnalyticsResponse>("/api/vigil/analytics/spans", {
      start_time_from: timeRange.start_time_from,
      start_time_to: timeRange.start_time_to,
      environment,
      resource,
      span_type: spanType,
      group_by: mode === "grouped" ? groupBy : undefined,
      bucket: mode === "timeseries" ? bucket : undefined,
    })
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setStatus("idle");
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setStatus("error");
        setError(
          reason instanceof VigilApiError
            ? { status: reason.status, message: reason.message }
            : { status: 0, message: "Unable to load telemetry analytics." },
        );
      });

    return () => {
      cancelled = true;
    };
  }, [timeRange.start_time_from, timeRange.start_time_to, environment, resource, spanType, mode, groupBy, bucket]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <TimeRangePicker value={range} onChange={setRange} />
        <FilterDropdown
          label="Environment"
          value={environment}
          options={environmentOptions.map((value) => ({ value, label: value }))}
          onChange={setEnvironment}
        />
        <FilterDropdown
          label="Resource"
          value={resource}
          options={resourceOptions.map((value) => ({ value, label: value }))}
          onChange={setResource}
        />
        <FilterDropdown
          label="Span type"
          value={spanType}
          options={spanTypeOptions.map((value) => ({ value, label: value }))}
          onChange={setSpanType}
        />
        <ModeSelector
          value={mode}
          onChange={setMode}
          options={[
            { value: "totals", label: "Totals" },
            { value: "grouped", label: "Grouped" },
            { value: "timeseries", label: "Over time" },
          ]}
        />
        {mode === "grouped" ? (
          <FilterDropdown
            label="Group by"
            value={groupBy}
            options={GROUP_BY_OPTIONS}
            placeholder="Environment"
            onChange={(value) => setGroupBy((value as SpanGroupBy | undefined) ?? "environment")}
          />
        ) : null}
      </div>

      {status === "loading" ? <SkeletonStatTiles /> : null}

      {status === "error" && error ? (
        <ErrorBanner title={titleForStatus(error.status)} message={error.message} />
      ) : null}

      {status === "idle" && data && mode === "totals" ? (
        (data.span_count ?? 0) === 0 ? (
          <EmptyState title="No spans in this time range" />
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <StatTile label="Spans" value={formatCount(data.span_count ?? 0)} />
            <StatTile
              label="Error Rate"
              value={formatPercent(data.error_rate ?? 0)}
              tone={(data.error_span_count ?? 0) > 0 ? "error" : "default"}
              sublabel={`${formatCount(data.error_span_count ?? 0)} errors`}
            />
            <StatTile label="p50 Latency" value={formatDuration(data.latency_ms?.p50 ?? 0)} />
            <StatTile label="p90 Latency" value={formatDuration(data.latency_ms?.p90 ?? 0)} />
            <StatTile label="p99 Latency" value={formatDuration(data.latency_ms?.p99 ?? 0)} />
          </div>
        )
      ) : null}

      {status === "idle" && data && mode === "grouped" ? (
        (data.groups ?? []).length === 0 ? (
          <EmptyState title="No data to group" />
        ) : (
          <SpanGroupedTable
            groups={data.groups ?? []}
            dimensionLabel={GROUP_BY_OPTIONS.find((option) => option.value === groupBy)?.label ?? "Group"}
          />
        )
      ) : null}

      {status === "idle" && data && mode === "timeseries" ? (
        <div className="rounded-lg border border-border p-4">
          <TimeSeriesChart buckets={data.buckets ?? []} bucket={bucket} />
        </div>
      ) : null}
    </div>
  );
}
