/**
 * Time-range presets shared by Overview, Traces, and Analytics. Bounded to
 * match the API's own window limits (apps/api/app/config.py:
 * max_query_window_days=7) -- there is no "all time" option, on purpose.
 */

export type TimeRangePreset = "1h" | "24h" | "7d";

export const DEFAULT_TIME_RANGE_PRESET: TimeRangePreset = "24h";

interface PresetDefinition {
  value: TimeRangePreset;
  label: string;
  hours: number;
}

export const TIME_RANGE_PRESETS: readonly PresetDefinition[] = [
  { value: "1h", label: "Last hour", hours: 1 },
  { value: "24h", label: "Last 24 hours", hours: 24 },
  { value: "7d", label: "Last 7 days", hours: 24 * 7 },
];

export function isTimeRangePreset(value: string | null | undefined): value is TimeRangePreset {
  return value === "1h" || value === "24h" || value === "7d";
}

export interface ResolvedTimeRange {
  start_time_from: string;
  start_time_to: string;
}

/** Preset -> concrete `[start_time_from, start_time_to)` ISO bounds. */
export function resolveTimeRange(preset: TimeRangePreset, now: Date = new Date()): ResolvedTimeRange {
  const definition = TIME_RANGE_PRESETS.find((p) => p.value === preset) ?? TIME_RANGE_PRESETS[1];
  const to = now;
  const from = new Date(to.getTime() - definition.hours * 60 * 60 * 1000);
  return { start_time_from: from.toISOString(), start_time_to: to.toISOString() };
}

/** Bucket granularity that keeps a time-series chart readable for a given preset. */
export function bucketForPreset(preset: TimeRangePreset): "hour" | "day" {
  return preset === "7d" ? "day" : "hour";
}
