// /v1/traces* -- mirrors apps/api/app/api/v1/traces.py, services/query.py and
// clickhouse/query_repository.py on D1.

import type { SpanOut, TraceDetailResponse, TraceListResponse, TraceStatus, TraceSummary } from "../../../dashboard/lib/api/types";
import { requireApiKey, requireProjectAccess } from "../auth";
import { parseJsonObject } from "../body";
import {
  DEFAULT_QUERY_WINDOW_HOURS,
  type Env,
  MAX_QUERY_WINDOW_DAYS,
  MAX_REQUEST_BODY_BYTES,
  MAX_SPANS_PER_PROJECT,
  MAX_SPANS_PER_TRACE_RESPONSE,
  RATE_LIMITS,
} from "../config";
import { evaluateIngestedSpans } from "../evaluation";
import { type SpanRow, transformRequest, validateTracesRequest } from "../ingest";
import { ApiError, isoTime, json, pyIsoformat, readBodyText } from "../http";
import { clientIp, enforceRateLimit } from "../rateLimit";
import { INVALID, Validator, pathHexId, queryAwareDatetime, queryBool, queryLimit } from "../validation";

// ---- ingestion ---------------------------------------------------------------

/** Rows for INSERT ... SELECT FROM json_each(?) -- one bound parameter per chunk. */
function storageRow(projectId: string, row: SpanRow, ingestedAt: number) {
  return {
    ...row,
    project_id: projectId,
    input_truncated: row.input_truncated ? 1 : 0,
    output_truncated: row.output_truncated ? 1 : 0,
    attributes: JSON.stringify(Object.fromEntries(row.attributes)),
    attributes_truncated: row.attributes_truncated ? 1 : 0,
    events: JSON.stringify(row.events.map((e) => ({ time: e.time, name: e.name, attributes: Object.fromEntries(e.attributes) }))),
    events_truncated: row.events_truncated ? 1 : 0,
    ingested_at: ingestedAt,
  };
}

const SPAN_COLUMNS = [
  "project_id", "trace_id", "span_id", "parent_span_id", "name", "span_type", "resource", "start_time",
  "end_time", "duration_ms", "status", "status_message", "input", "input_size_bytes", "input_truncated",
  "output", "output_size_bytes", "output_truncated", "attributes", "attributes_truncated", "events",
  "events_truncated", "llm_provider", "llm_model", "llm_input_tokens", "llm_output_tokens",
  "llm_total_tokens", "llm_cost_micros", "environment", "release", "ingested_at",
];
const INSERT_SPANS_SQL = `INSERT OR REPLACE INTO spans (${SPAN_COLUMNS.join(", ")})
  SELECT ${SPAN_COLUMNS.map((c) => `json_extract(value, '$.${c}')`).join(", ")} FROM json_each(?)`;

/** D1 caps a bound value at 2 MB; split the JSON payload well under that. */
function chunkJson(rows: object[], maxBytes = 1_500_000): string[] {
  const chunks: string[] = [];
  let current: string[] = [];
  let size = 2;
  for (const row of rows) {
    const text = JSON.stringify(row);
    if (current.length > 0 && size + text.length + 1 > maxBytes) {
      chunks.push(`[${current.join(",")}]`);
      current = [];
      size = 2;
    }
    current.push(text);
    size += text.length + 1;
  }
  if (current.length > 0) chunks.push(`[${current.join(",")}]`);
  return chunks;
}

export async function ingestTraces(request: Request, env: Env): Promise<Response> {
  // Order matches apps/api: body-size middleware (413), then the API-key and
  // rate-limit dependency (401/429), then body validation (422).
  const text = await readBodyText(request, MAX_REQUEST_BODY_BYTES);
  const key = await requireApiKey(request, env);
  await enforceRateLimit(env, RATE_LIMITS.ingestPerKey, key.apiKeyId);
  await enforceRateLimit(env, RATE_LIMITS.ingestPerIp, clientIp(request, env));
  const rows = transformRequest(validateTracesRequest(parseJsonObject(text)));

  // Last occurrence of a (trace_id, span_id) in one batch wins.
  const unique = [...new Map(rows.map((r) => [`${r.trace_id}:${r.span_id}`, r])).values()];
  const keysJson = JSON.stringify(unique.map((r) => [r.trace_id, r.span_id]));
  const cap = await env.DB.prepare(
    `SELECT (SELECT COUNT(*) FROM spans WHERE project_id = ?1) AS stored,
            (SELECT COUNT(*) FROM json_each(?2) j WHERE NOT EXISTS (
               SELECT 1 FROM spans s WHERE s.project_id = ?1
                 AND s.trace_id = json_extract(j.value, '$[0]') AND s.span_id = json_extract(j.value, '$[1]'))) AS incoming`,
  )
    .bind(key.projectId, keysJson)
    .first<{ stored: number; incoming: number }>();
  if ((cap?.stored ?? 0) + (cap?.incoming ?? 0) > MAX_SPANS_PER_PROJECT) {
    throw new ApiError(
      403,
      `Public demo limit reached: a project stores at most ${MAX_SPANS_PER_PROJECT} spans ` +
        `(this project has ${cap?.stored ?? 0}). Demo telemetry is deleted after 7 days; ` +
        "create another project to keep experimenting.",
    );
  }

  const ingestedAt = Date.now();
  const statements = chunkJson(unique.map((r) => storageRow(key.projectId, r, ingestedAt))).map((chunk) =>
    env.DB.prepare(INSERT_SPANS_SQL).bind(chunk),
  );
  await env.DB.batch(statements);
  await evaluateIngestedSpans(env, key.projectId, unique);
  return json({ accepted: rows.length, request_id: crypto.randomUUID().replace(/-/g, "") });
}

// ---- queries -----------------------------------------------------------------

interface Window {
  from: number;
  to: number;
}

function resolveWindow(from: number | null, to: number | null): Window {
  const resolvedTo = to ?? Date.now();
  const resolvedFrom = from ?? resolvedTo - DEFAULT_QUERY_WINDOW_HOURS * 3600e3;
  if (resolvedFrom > resolvedTo) throw new ApiError(422, "start_time_from must not be after start_time_to.");
  if (resolvedTo - resolvedFrom > MAX_QUERY_WINDOW_DAYS * 86400e3) {
    throw new ApiError(
      422,
      `Time window must not exceed ${MAX_QUERY_WINDOW_DAYS} days; narrow start_time_from/start_time_to.`,
    );
  }
  return { from: resolvedFrom, to: resolvedTo };
}

/**
 * Parses start_time_from/start_time_to (query validation, collected into `v`);
 * call `resolve()` after v.throwIfInvalid() and any service-level checks that
 * production runs before resolve_time_window.
 */
export function windowParams(url: URL, v: Validator): { resolve: () => Window } {
  const from = queryAwareDatetime(url.searchParams.get("start_time_from"), "start_time_from", v);
  const to = queryAwareDatetime(url.searchParams.get("start_time_to"), "start_time_to", v);
  return { resolve: () => resolveWindow(from === INVALID ? null : from, to === INVALID ? null : to) };
}

function base64UrlEncode(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_");
}

function base64UrlDecode(text: string): string {
  const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
  return new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0)));
}

/** services/query.py's cursor: urlsafe base64 of {"start_time": isoformat, "trace_id"}. */
export function encodeCursor(fields: Record<string, string>): string {
  return base64UrlEncode(JSON.stringify(fields));
}

export function decodeCursor(cursor: string, timeKey: string): { ms: number; data: Record<string, unknown> } {
  const malformed = () => new ApiError(422, "Malformed pagination cursor.");
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(base64UrlDecode(cursor));
  } catch {
    throw malformed();
  }
  const time = data?.[timeKey];
  const v = new Validator();
  const parsed = typeof time === "string" ? queryAwareDatetime(time, timeKey, v) : INVALID;
  if (parsed === INVALID || parsed === null) throw malformed();
  return { ms: parsed, data };
}

function deriveStatus(errorCount: number, rootCount: number): TraceStatus {
  if (errorCount > 0) return "error";
  if (rootCount > 0) return "ok";
  return "unknown";
}

export async function listTraces(request: Request, env: Env): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const url = new URL(request.url);
  const v = new Validator();
  const hasError = queryBool(url.searchParams.get("has_error"), "has_error", v);
  const limit = queryLimit(url.searchParams.get("limit"), v);
  const windowQuery = windowParams(url, v);
  v.throwIfInvalid();
  const window = windowQuery.resolve();
  const environment = url.searchParams.get("environment");
  const resource = url.searchParams.get("resource");
  const cursorRaw = url.searchParams.get("cursor");
  let cursor: { ms: number; traceId: string } | null = null;
  if (cursorRaw) {
    const decoded = decodeCursor(cursorRaw, "start_time");
    const traceId = decoded.data.trace_id;
    if (typeof traceId !== "string" || !/^[0-9a-fA-F]{32}$/.test(traceId)) throw new ApiError(422, "Malformed pagination cursor.");
    cursor = { ms: decoded.ms, traceId: traceId.toLowerCase() };
  }

  const where = ["project_id = ?", "start_time >= ?", "start_time < ?"];
  const params: unknown[] = [access.projectId, window.from, window.to];
  if (environment !== null) (where.push("environment = ?"), params.push(environment));
  if (resource !== null) (where.push("resource = ?"), params.push(resource));
  const having: string[] = [];
  if (hasError === true) having.push("error_span_count > 0");
  if (hasError === false) having.push("error_span_count = 0");
  if (cursor) {
    having.push("(trace_start_time < ? OR (trace_start_time = ? AND trace_id < ?))");
    params.push(cursor.ms, cursor.ms, cursor.traceId);
  }
  params.push((limit as number) + 1);
  const { results } = await env.DB.prepare(
    `SELECT trace_id,
            MIN(start_time) AS trace_start_time,
            MAX(end_time) AS trace_end_time,
            COUNT(*) AS span_count,
            SUM(status = 'error') AS error_span_count,
            SUM(parent_span_id IS NULL) AS root_span_count,
            MAX(CASE WHEN parent_span_id IS NULL THEN name END) AS root_span_name,
            MIN(environment) AS trace_environment,
            MIN(resource) AS trace_resource
       FROM spans WHERE ${where.join(" AND ")}
      GROUP BY trace_id
      ${having.length ? `HAVING ${having.join(" AND ")}` : ""}
      ORDER BY trace_start_time DESC, trace_id DESC
      LIMIT ?`,
  )
    .bind(...params)
    .all<{
      trace_id: string;
      trace_start_time: number;
      trace_end_time: number;
      span_count: number;
      error_span_count: number;
      root_span_count: number;
      root_span_name: string | null;
      trace_environment: string;
      trace_resource: string;
    }>();

  const traces: TraceSummary[] = results.slice(0, limit as number).map((r) => ({
    trace_id: r.trace_id,
    start_time: isoTime(r.trace_start_time),
    end_time: isoTime(r.trace_end_time),
    duration_ms: r.trace_end_time - r.trace_start_time,
    status: deriveStatus(r.error_span_count, r.root_span_count),
    span_count: r.span_count,
    error_span_count: r.error_span_count,
    root_span_name: r.root_span_name || null,
    environment: r.trace_environment,
    resource: r.trace_resource,
  }));
  const last = results.length > (limit as number) ? results[(limit as number) - 1] : null;
  const body: TraceListResponse = {
    traces,
    next_cursor: last ? encodeCursor({ start_time: pyIsoformat(last.trace_start_time), trace_id: last.trace_id }) : null,
  };
  return json(body);
}

interface StoredSpan {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  span_type: string;
  resource: string;
  start_time: number;
  end_time: number;
  duration_ms: number;
  status: SpanOut["status"];
  status_message: string | null;
  input: string | null;
  input_size_bytes: number;
  input_truncated: number;
  output: string | null;
  output_size_bytes: number;
  output_truncated: number;
  attributes: string;
  attributes_truncated: number;
  events: string;
  events_truncated: number;
  llm_provider: string | null;
  llm_model: string | null;
  llm_input_tokens: number | null;
  llm_output_tokens: number | null;
  llm_total_tokens: number | null;
  llm_cost_micros: number | null;
  environment: string;
  release: string | null;
}

/** Decimal64(6) as clickhouse_connect returns it: always 6 decimal places. */
export function formatMicros(micros: number | null): string | null {
  if (micros === null) return null;
  const whole = Math.trunc(micros / 1e6);
  const frac = String(micros % 1e6).padStart(6, "0");
  return `${whole}.${frac}`;
}

function spanOut(s: StoredSpan): SpanOut {
  return {
    span_id: s.span_id,
    parent_span_id: s.parent_span_id,
    name: s.name,
    span_type: s.span_type,
    resource: s.resource,
    start_time: isoTime(s.start_time),
    end_time: isoTime(s.end_time),
    duration_ms: s.duration_ms,
    status: s.status,
    status_message: s.status_message,
    input: s.input,
    input_size_bytes: s.input_size_bytes,
    input_truncated: s.input_truncated === 1,
    output: s.output,
    output_size_bytes: s.output_size_bytes,
    output_truncated: s.output_truncated === 1,
    attributes: JSON.parse(s.attributes),
    attributes_truncated: s.attributes_truncated === 1,
    events: (JSON.parse(s.events) as { time: number; name: string; attributes: Record<string, string> }[]).map((e) => ({
      time: isoTime(e.time),
      name: e.name,
      attributes: e.attributes,
    })),
    events_truncated: s.events_truncated === 1,
    llm_provider: s.llm_provider,
    llm_model: s.llm_model,
    llm_input_tokens: s.llm_input_tokens,
    llm_output_tokens: s.llm_output_tokens,
    llm_total_tokens: s.llm_total_tokens,
    llm_cost_usd: formatMicros(s.llm_cost_micros),
    environment: s.environment,
    release: s.release,
  };
}

/** Optional `start_date` hint: production filters on toDate(start_time) = start_date (UTC). */
function startDateFilter(url: URL, v: Validator): [number, number] | null {
  const raw = url.searchParams.get("start_date");
  if (raw === null) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  const ms = m ? Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : NaN;
  if (!m || Number.isNaN(ms) || new Date(ms).getUTCDate() !== Number(m[3])) {
    v.add("date_from_datetime_parsing", ["query", "start_date"], "Input should be a valid date or datetime, invalid date format");
    return null;
  }
  return [ms, ms + 86400e3];
}

function traceScope(projectId: string, traceId: string, day: [number, number] | null): [string, unknown[]] {
  const where = ["project_id = ?", "trace_id = ?"];
  const params: unknown[] = [projectId, traceId];
  if (day) (where.push("start_time >= ? AND start_time < ?"), params.push(day[0], day[1]));
  return [where.join(" AND "), params];
}

export async function getTrace(request: Request, env: Env, traceIdRaw: string): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const v = new Validator();
  const traceId = pathHexId(traceIdRaw, "trace_id", v);
  const day = startDateFilter(new URL(request.url), v);
  v.throwIfInvalid();
  const [where, params] = traceScope(access.projectId, traceId as string, day);
  const [summaryResult, spansResult] = await env.DB.batch([
    env.DB.prepare(
      `SELECT COUNT(*) AS total_span_count, SUM(status = 'error') AS error_span_count,
              SUM(parent_span_id IS NULL) AS root_span_count,
              MIN(start_time) AS trace_start_time, MAX(end_time) AS trace_end_time
         FROM spans WHERE ${where}`,
    ).bind(...params),
    env.DB.prepare(`SELECT * FROM spans WHERE ${where} ORDER BY start_time ASC, span_id ASC LIMIT ?`).bind(
      ...params,
      MAX_SPANS_PER_TRACE_RESPONSE + 1,
    ),
  ]);
  const summary = summaryResult.results[0] as {
    total_span_count: number;
    error_span_count: number;
    root_span_count: number;
    trace_start_time: number;
    trace_end_time: number;
  };
  if (!summary || summary.total_span_count === 0) throw new ApiError(404, "Trace not found.");
  const rows = spansResult.results as unknown as StoredSpan[];
  const spans = rows.slice(0, MAX_SPANS_PER_TRACE_RESPONSE).map(spanOut);
  const body: TraceDetailResponse = {
    trace_id: traceId as string,
    status: deriveStatus(summary.error_span_count, summary.root_span_count),
    start_time: isoTime(summary.trace_start_time),
    end_time: isoTime(summary.trace_end_time),
    duration_ms: summary.trace_end_time - summary.trace_start_time,
    span_count: spans.length,
    total_span_count: summary.total_span_count,
    truncated: rows.length > MAX_SPANS_PER_TRACE_RESPONSE,
    spans,
  };
  return json(body);
}

export async function getSpan(request: Request, env: Env, traceIdRaw: string, spanIdRaw: string): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const v = new Validator();
  const traceId = pathHexId(traceIdRaw, "trace_id", v);
  const spanId = pathHexId(spanIdRaw, "span_id", v);
  const day = startDateFilter(new URL(request.url), v);
  v.throwIfInvalid();
  const [where, params] = traceScope(access.projectId, traceId as string, day);
  const row = await env.DB.prepare(`SELECT * FROM spans WHERE ${where} AND span_id = ? LIMIT 1`)
    .bind(...params, spanId)
    .first<StoredSpan>();
  if (!row) throw new ApiError(404, "Span not found.");
  return json(spanOut(row));
}
