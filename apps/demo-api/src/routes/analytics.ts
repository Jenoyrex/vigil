// /v1/analytics/* -- mirrors apps/api/app/services/analytics.py and
// clickhouse/analytics_repository.py. Aggregation happens in the Worker over
// the matching rows (bounded by MAX_SPANS_PER_PROJECT), since SQLite has no
// quantile function.

import type {
  LatencyPercentiles,
  LlmGroupBy,
  LlmUsageResponse,
  SpanAnalyticsResponse,
  SpanBucket,
  SpanGroupBy,
} from "../../../dashboard/lib/api/types";
import { requireProjectAccess } from "../auth";
import { type Env, MAX_ANALYTICS_GROUPS } from "../config";
import { ApiError, isoTime, json } from "../http";
import { Validator, queryLiteral } from "../validation";
import { formatMicros, windowParams } from "./traces";

const SPAN_GROUP_BY = ["environment", "span_type", "release", "resource"] as const;
const BUCKETS = ["hour", "day"] as const;
const LLM_GROUP_BY = ["llm_provider", "llm_model", "environment"] as const;
const BUCKET_MS: Record<SpanBucket, number> = { hour: 3600e3, day: 86400e3 };

/**
 * ClickHouse `quantile(level)`: reservoir sample (exact below 8192 values)
 * with linear interpolation between neighbours. Empty input -> NaN -> 0.0.
 */
export function quantile(sorted: number[], level: number): number {
  if (sorted.length === 0) return 0;
  const index = Math.max(0, Math.min(sorted.length - 1, level * (sorted.length - 1)));
  const lo = Math.floor(index);
  const hi = Math.ceil(index);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (index - lo);
}

interface SpanMetricRow {
  duration_ms: number;
  status: string;
  group_value: string | null;
  start_time: number;
}

function metrics(rows: SpanMetricRow[]) {
  const durations = rows.map((r) => r.duration_ms).sort((a, b) => a - b);
  const spanCount = rows.length;
  const errorSpanCount = rows.filter((r) => r.status === "error").length;
  const latency: LatencyPercentiles = {
    p50: quantile(durations, 0.5),
    p90: quantile(durations, 0.9),
    p99: quantile(durations, 0.99),
  };
  return { span_count: spanCount, error_span_count: errorSpanCount, error_rate: spanCount ? errorSpanCount / spanCount : 0, latency_ms: latency };
}

function groupBy<T>(rows: T[], key: (row: T) => string | number): Map<string | number, T[]> {
  const groups = new Map<string | number, T[]>();
  for (const row of rows) {
    const k = key(row);
    const list = groups.get(k);
    if (list) list.push(row);
    else groups.set(k, [row]);
  }
  return groups;
}

export async function spanAnalytics(request: Request, env: Env): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const url = new URL(request.url);
  const v = new Validator();
  const group = queryLiteral(url.searchParams.get("group_by"), "group_by", SPAN_GROUP_BY, v) as SpanGroupBy | null;
  const bucket = queryLiteral(url.searchParams.get("bucket"), "bucket", BUCKETS, v) as SpanBucket | null;
  const windowQuery = windowParams(url, v);
  v.throwIfInvalid();
  if (group !== null && bucket !== null) throw new ApiError(422, "group_by and bucket are mutually exclusive.");
  const window = windowQuery.resolve();

  const where = ["project_id = ?", "start_time >= ?", "start_time < ?"];
  const params: unknown[] = [access.projectId, window.from, window.to];
  for (const name of ["environment", "resource", "span_type"] as const) {
    const value = url.searchParams.get(name);
    if (value !== null) (where.push(`${name} = ?`), params.push(value));
  }
  const groupColumn = group ?? "NULL"; // validated literal, safe to interpolate
  const { results } = await env.DB.prepare(
    `SELECT duration_ms, status, start_time, ${groupColumn} AS group_value FROM spans WHERE ${where.join(" AND ")}`,
  )
    .bind(...params)
    .all<SpanMetricRow>();

  const body: SpanAnalyticsResponse = {
    start_time_from: isoTime(window.from),
    start_time_to: isoTime(window.to),
    group_by: group,
    bucket,
    span_count: null,
    error_span_count: null,
    error_rate: null,
    latency_ms: null,
    groups: null,
    buckets: null,
  };
  if (group !== null) {
    // Production would fail on a NULL `release` group (value: str); the demo reports "".
    body.groups = [...groupBy(results, (r) => r.group_value ?? "")]
      .map(([value, rows]) => ({ value: String(value), ...metrics(rows) }))
      .sort((a, b) => b.span_count - a.span_count || a.value.localeCompare(b.value))
      .slice(0, MAX_ANALYTICS_GROUPS);
  } else if (bucket !== null) {
    const size = BUCKET_MS[bucket];
    body.buckets = [...groupBy(results, (r) => Math.floor(r.start_time / size) * size)]
      .sort(([a], [b]) => (a as number) - (b as number))
      .map(([start, rows]) => ({ bucket_start: isoTime(start as number), ...metrics(rows) }));
  } else {
    const m = metrics(results);
    Object.assign(body, { span_count: m.span_count, error_span_count: m.error_span_count, error_rate: m.error_rate, latency_ms: m.latency_ms });
  }
  return json(body);
}

interface LlmRow {
  group_value: string | null;
  llm_span_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_cost_micros: number;
}

export async function llmUsage(request: Request, env: Env): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const url = new URL(request.url);
  const v = new Validator();
  const group = queryLiteral(url.searchParams.get("group_by"), "group_by", LLM_GROUP_BY, v) as LlmGroupBy | null;
  const windowQuery = windowParams(url, v);
  v.throwIfInvalid();
  const window = windowQuery.resolve();

  const where = ["project_id = ?", "start_time >= ?", "start_time < ?", "llm_provider IS NOT NULL"];
  const params: unknown[] = [access.projectId, window.from, window.to];
  const environment = url.searchParams.get("environment");
  if (environment !== null) (where.push("environment = ?"), params.push(environment));
  const select = `COUNT(*) AS llm_span_count,
    COALESCE(SUM(llm_input_tokens), 0) AS total_input_tokens,
    COALESCE(SUM(llm_output_tokens), 0) AS total_output_tokens,
    COALESCE(SUM(llm_total_tokens), 0) AS total_tokens,
    COALESCE(SUM(llm_cost_micros), 0) AS total_cost_micros`;
  const sql = group
    ? `SELECT COALESCE(${group}, '') AS group_value, ${select} FROM spans WHERE ${where.join(" AND ")}
       GROUP BY group_value ORDER BY total_cost_micros DESC, group_value LIMIT ${MAX_ANALYTICS_GROUPS}`
    : `SELECT NULL AS group_value, ${select} FROM spans WHERE ${where.join(" AND ")}`;
  const { results } = await env.DB.prepare(sql).bind(...params).all<LlmRow>();

  const body: LlmUsageResponse = {
    start_time_from: isoTime(window.from),
    start_time_to: isoTime(window.to),
    group_by: group,
    llm_span_count: null,
    total_input_tokens: null,
    total_output_tokens: null,
    total_tokens: null,
    total_cost_usd: null,
    groups: null,
  };
  if (group) {
    body.groups = results.map((r) => ({
      value: r.group_value ?? "",
      llm_span_count: r.llm_span_count,
      total_input_tokens: r.total_input_tokens,
      total_output_tokens: r.total_output_tokens,
      total_tokens: r.total_tokens,
      total_cost_usd: formatMicros(r.total_cost_micros)!,
    }));
  } else {
    const r = results[0];
    Object.assign(body, {
      llm_span_count: r.llm_span_count,
      total_input_tokens: r.total_input_tokens,
      total_output_tokens: r.total_output_tokens,
      total_tokens: r.total_tokens,
      total_cost_usd: formatMicros(r.total_cost_micros),
    });
  }
  return json(body);
}
