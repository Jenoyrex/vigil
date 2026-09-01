"use client";

import { useEffect, useMemo, useState } from "react";

import { TimeRangePicker } from "@/components/layout/TimeRangePicker";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { FilterDropdown } from "@/components/ui/FilterDropdown";
import { SkeletonStatTiles } from "@/components/ui/Skeleton";
import { StatTile } from "@/components/ui/StatTile";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import { VigilApiError, type LlmGroupBy, type LlmUsageResponse } from "@/lib/api/types";
import { titleForStatus } from "@/lib/errorMessages";
import { useDimensionOptions } from "@/lib/hooks/useDimensionOptions";
import { formatCost, formatCount } from "@/lib/format";
import { resolveTimeRange, type TimeRangePreset } from "@/lib/time-range";

import { LlmUsageGroupedTable } from "./LlmGroupedTable";
import { ModeSelector } from "./ModeSelector";

/** No "Over time" mode -- GET /v1/analytics/llm-usage has no `bucket`
 * parameter (unlike GET /v1/analytics/spans); only Totals/Grouped exist. */
type Mode = "totals" | "grouped";

const GROUP_BY_OPTIONS: { value: LlmGroupBy; label: string }[] = [
  { value: "llm_provider", label: "Provider" },
  { value: "llm_model", label: "Model" },
  { value: "environment", label: "Environment" },
];

interface FetchError {
  status: number;
  message: string;
}

export function LlmUsagePanel() {
  const [range, setRange] = useState<TimeRangePreset>("24h");
  const [environment, setEnvironment] = useState<string | undefined>(undefined);
  const [mode, setMode] = useState<Mode>("totals");
  const [groupBy, setGroupBy] = useState<LlmGroupBy>("llm_provider");

  const [data, setData] = useState<LlmUsageResponse | null>(null);
  const [status, setStatus] = useState<"loading" | "idle" | "error">("loading");
  const [error, setError] = useState<FetchError | null>(null);

  // Memoized on `range` alone -- resolveTimeRange(range) defaults "now" to
  // `new Date()`, so calling it unmemoized in the render body would return
  // a new object (with a new start_time_to) on every render, which would
  // then retrigger the effect below via its timeRange.* dependencies,
  // causing an infinite fetch loop.
  const timeRange = useMemo(() => resolveTimeRange(range), [range]);
  const environmentOptions = useDimensionOptions("environment", timeRange);

  useEffect(() => {
    let cancelled = false;
    // Runs on mount and on every filter/mode change -- there is no user
    // event to hang the "start loading" state update on instead.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStatus("loading");
    setError(null);

    fetchVigilProxy<LlmUsageResponse>("/api/vigil/analytics/llm-usage", {
      start_time_from: timeRange.start_time_from,
      start_time_to: timeRange.start_time_to,
      environment,
      group_by: mode === "grouped" ? groupBy : undefined,
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
            : { status: 0, message: "Unable to load LLM usage analytics." },
        );
      });

    return () => {
      cancelled = true;
    };
  }, [timeRange.start_time_from, timeRange.start_time_to, environment, mode, groupBy]);

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
        <ModeSelector
          value={mode}
          onChange={setMode}
          options={[
            { value: "totals", label: "Totals" },
            { value: "grouped", label: "Grouped" },
          ]}
        />
        {mode === "grouped" ? (
          <FilterDropdown
            label="Group by"
            value={groupBy}
            options={GROUP_BY_OPTIONS}
            placeholder="Provider"
            onChange={(value) => setGroupBy((value as LlmGroupBy | undefined) ?? "llm_provider")}
          />
        ) : null}
      </div>

      {status === "loading" ? <SkeletonStatTiles count={4} /> : null}

      {status === "error" && error ? (
        <ErrorBanner title={titleForStatus(error.status)} message={error.message} />
      ) : null}

      {status === "idle" && data && mode === "totals" ? (
        (data.llm_span_count ?? 0) === 0 ? (
          <EmptyState title="No LLM spans in this time range" description="Spans with llm_provider set will appear here." />
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile label="LLM Spans" value={formatCount(data.llm_span_count ?? 0)} />
            <StatTile label="Input Tokens" value={formatCount(data.total_input_tokens ?? 0)} />
            <StatTile label="Output Tokens" value={formatCount(data.total_output_tokens ?? 0)} />
            <StatTile label="Total Cost" value={formatCost(data.total_cost_usd ?? null)} />
          </div>
        )
      ) : null}

      {status === "idle" && data && mode === "grouped" ? (
        (data.groups ?? []).length === 0 ? (
          <EmptyState title="No LLM spans to group" />
        ) : (
          <LlmUsageGroupedTable
            groups={data.groups ?? []}
            dimensionLabel={GROUP_BY_OPTIONS.find((option) => option.value === groupBy)?.label ?? "Group"}
          />
        )
      ) : null}
    </div>
  );
}
