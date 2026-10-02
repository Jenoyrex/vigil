// /v1/evaluations/* and span evaluation results -- mirrors
// apps/api/app/api/v1/evaluations.py and services/evaluations.py.

import type {
  EvaluationJobListResponse,
  EvaluationJobOut,
  EvaluationResultOut,
  EvaluatorConfigListResponse,
  EvaluatorConfigOut,
  SpanEvaluationsResponse,
} from "../../../dashboard/lib/api/types";
import { requireProjectAccess } from "../auth";
import { readJsonObject } from "../body";
import { DEFAULT_MAX_RETRIES, DEFAULT_SAMPLING_RATE, type Env } from "../config";
import { ApiError, type Loc, isoTime, isoTimeOrNull, json, pyIsoformat } from "../http";
import { JsonNumber } from "../pyjson";
import { decodeCursor, encodeCursor, formatMicros } from "./traces";
import {
  INVALID,
  type Maybe,
  type Obj,
  Validator,
  checkEvaluatorName,
  coerceFloat,
  coerceInt,
  parseUuid,
  pathHexId,
  queryLimit,
  queryLiteral,
} from "../validation";

const JOB_STATUSES = ["pending", "running", "succeeded", "failed", "dead_letter"] as const;

/** Not offered by the demo: the BGE model does not fit a free Worker (ADR 009). */
const UNSUPPORTED_EVALUATORS: Record<string, string> = {
  relevance_embedding:
    "relevance_embedding (BGE embedding relevance) is not available in the public demo. " +
    "Use the 'relevance' evaluator (TF-IDF cosine), which runs on every sampled LLM span.",
};

interface ConfigRow {
  evaluator_name: string;
  enabled: number;
  sampling_rate: number;
  threshold: number | null;
  max_retries: number;
  created_at: number;
  updated_at: number;
}

const configOut = (c: ConfigRow): EvaluatorConfigOut => ({
  evaluator_name: c.evaluator_name,
  enabled: c.enabled === 1,
  sampling_rate: c.sampling_rate,
  threshold: c.threshold,
  max_retries: c.max_retries,
  created_at: isoTime(c.created_at),
  updated_at: isoTime(c.updated_at),
});

const CONFIG_COLUMNS = "evaluator_name, enabled, sampling_rate, threshold, max_retries, created_at, updated_at";

function evaluatorNameFromPath(raw: string): string {
  const v = new Validator();
  const name = checkEvaluatorName(raw, ["path", "evaluator_name"], v);
  v.throwIfInvalid();
  return name as string;
}

export async function listConfigs(request: Request, env: Env): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const { results } = await env.DB.prepare(`SELECT ${CONFIG_COLUMNS} FROM evaluator_configs WHERE project_id = ? ORDER BY evaluator_name`)
    .bind(access.projectId)
    .all<ConfigRow>();
  const body: EvaluatorConfigListResponse = { configs: results.map(configOut) };
  return json(body);
}

export async function getConfig(request: Request, env: Env, nameRaw: string): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const name = evaluatorNameFromPath(nameRaw);
  const row = await env.DB.prepare(`SELECT ${CONFIG_COLUMNS} FROM evaluator_configs WHERE project_id = ? AND evaluator_name = ?`)
    .bind(access.projectId, name)
    .first<ConfigRow>();
  if (!row) throw new ApiError(404, "This evaluator has never been configured for this project.");
  return json(configOut(row));
}

/** Pydantic lax `bool`. */
function coerceBool(value: unknown, loc: Loc, v: Validator): Maybe<boolean> {
  if (typeof value === "boolean") return value;
  if (value instanceof JsonNumber && (value.value === 0 || value.value === 1)) return value.value === 1;
  if (typeof value === "string") {
    const t = value.trim().toLowerCase();
    if (["1", "on", "t", "true", "y", "yes"].includes(t)) return true;
    if (["0", "off", "f", "false", "n", "no"].includes(t)) return false;
    return v.add("bool_parsing", loc, "Input should be a valid boolean, unable to interpret input");
  }
  return v.add("bool_type", loc, "Input should be a valid boolean");
}

function parseUpsert(body: Obj) {
  const v = new Validator();
  const enabled = "enabled" in body ? coerceBool(body.enabled, ["body", "enabled"], v) : v.add("missing", ["body", "enabled"], "Field required");
  let samplingRate: Maybe<number> = DEFAULT_SAMPLING_RATE;
  if (body.sampling_rate !== undefined) {
    samplingRate = coerceFloat(body.sampling_rate, ["body", "sampling_rate"], v);
    if (samplingRate !== INVALID && !(samplingRate >= 0)) samplingRate = v.add("greater_than_equal", ["body", "sampling_rate"], "Input should be greater than or equal to 0");
    else if (samplingRate !== INVALID && samplingRate > 1) samplingRate = v.add("less_than_equal", ["body", "sampling_rate"], "Input should be less than or equal to 1");
  }
  let threshold: Maybe<number | null> = null;
  if (body.threshold !== undefined && body.threshold !== null) threshold = coerceFloat(body.threshold, ["body", "threshold"], v);
  let maxRetries: Maybe<number> = DEFAULT_MAX_RETRIES;
  if (body.max_retries !== undefined) {
    maxRetries = coerceInt(body.max_retries, ["body", "max_retries"], v);
    if (maxRetries !== INVALID && maxRetries < 0) maxRetries = v.add("greater_than_equal", ["body", "max_retries"], "Input should be greater than or equal to 0");
  }
  v.throwIfInvalid();
  return { enabled: enabled as boolean, samplingRate: samplingRate as number, threshold: threshold as number | null, maxRetries: maxRetries as number };
}

export async function upsertConfig(request: Request, env: Env, nameRaw: string): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const name = evaluatorNameFromPath(nameRaw);
  const input = parseUpsert(await readJsonObject(request, 64 * 1024));
  if (UNSUPPORTED_EVALUATORS[name]) throw new ApiError(422, UNSUPPORTED_EVALUATORS[name]);
  const now = Date.now();
  const row = await env.DB.prepare(
    `INSERT INTO evaluator_configs (id, project_id, evaluator_name, enabled, sampling_rate, threshold, max_retries, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (project_id, evaluator_name) DO UPDATE SET
       enabled = excluded.enabled, sampling_rate = excluded.sampling_rate, threshold = excluded.threshold,
       max_retries = excluded.max_retries, updated_at = excluded.updated_at
     RETURNING ${CONFIG_COLUMNS}`,
  )
    .bind(crypto.randomUUID(), access.projectId, name, input.enabled ? 1 : 0, input.samplingRate, input.threshold, input.maxRetries, now, now)
    .first<ConfigRow>();
  return json(configOut(row as ConfigRow));
}

interface JobDbRow extends Omit<EvaluationJobOut, "next_attempt_at" | "claimed_at" | "created_at" | "updated_at"> {
  next_attempt_at: number | null;
  claimed_at: number | null;
  created_at: number;
  updated_at: number;
}

const jobOut = (j: JobDbRow): EvaluationJobOut => ({
  id: j.id,
  trace_id: j.trace_id,
  span_id: j.span_id,
  evaluator_name: j.evaluator_name,
  evaluator_version: j.evaluator_version,
  status: j.status,
  attempt_count: j.attempt_count,
  max_retries: j.max_retries,
  next_attempt_at: isoTimeOrNull(j.next_attempt_at),
  claimed_at: isoTimeOrNull(j.claimed_at),
  claimed_by: j.claimed_by,
  last_error: j.last_error,
  created_at: isoTime(j.created_at),
  updated_at: isoTime(j.updated_at),
});

export async function listJobs(request: Request, env: Env): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const url = new URL(request.url);
  const v = new Validator();
  const status = queryLiteral(url.searchParams.get("status"), "status", JOB_STATUSES, v);
  const nameRaw = url.searchParams.get("evaluator_name");
  const evaluatorName = nameRaw === null ? null : checkEvaluatorName(nameRaw, ["query", "evaluator_name"], v);
  const limit = queryLimit(url.searchParams.get("limit"), v);
  v.throwIfInvalid();

  const where = ["project_id = ?"];
  const params: unknown[] = [access.projectId];
  if (status !== null) (where.push("status = ?"), params.push(status));
  if (evaluatorName !== null) (where.push("evaluator_name = ?"), params.push(evaluatorName));
  const cursorRaw = url.searchParams.get("cursor");
  if (cursorRaw) {
    const decoded = decodeCursor(cursorRaw, "created_at");
    const id = parseUuid(typeof decoded.data.id === "string" ? decoded.data.id : null);
    if (id === null) throw new ApiError(422, "Malformed pagination cursor.");
    where.push("(created_at < ? OR (created_at = ? AND id < ?))");
    params.push(decoded.ms, decoded.ms, id);
  }
  const { results } = await env.DB.prepare(
    `SELECT * FROM evaluation_jobs WHERE ${where.join(" AND ")} ORDER BY created_at DESC, id DESC LIMIT ?`,
  )
    .bind(...params, (limit as number) + 1)
    .all<JobDbRow>();
  const jobs = results.slice(0, limit as number);
  const last = results.length > (limit as number) ? jobs.at(-1) : undefined;
  const body: EvaluationJobListResponse = {
    jobs: jobs.map(jobOut),
    next_cursor: last ? encodeCursor({ created_at: pyIsoformat(last.created_at), id: last.id }) : null,
  };
  return json(body);
}

interface ResultDbRow {
  evaluation_id: string;
  trace_id: string;
  span_id: string;
  evaluator_name: string;
  evaluator_version: string;
  score: number | null;
  label: string;
  explanation: string;
  evaluator_model: string | null;
  evaluator_provider: string | null;
  evaluation_latency_ms: number;
  evaluation_cost_micros: number | null;
  job_created_at: number;
  written_at: number;
}

export async function listSpanEvaluations(request: Request, env: Env, traceIdRaw: string, spanIdRaw: string): Promise<Response> {
  const access = await requireProjectAccess(request, env);
  const v = new Validator();
  const traceId = pathHexId(traceIdRaw, "trace_id", v);
  const spanId = pathHexId(spanIdRaw, "span_id", v);
  v.throwIfInvalid();
  const { results } = await env.DB.prepare(
    `SELECT * FROM evaluation_results WHERE project_id = ? AND trace_id = ? AND span_id = ?
      ORDER BY evaluator_name, evaluator_version`,
  )
    .bind(access.projectId, traceId, spanId)
    .all<ResultDbRow>();
  const body: SpanEvaluationsResponse = {
    results: results.map(
      (r): EvaluationResultOut => ({
        evaluation_id: r.evaluation_id,
        trace_id: r.trace_id,
        span_id: r.span_id,
        evaluator_name: r.evaluator_name,
        evaluator_version: r.evaluator_version,
        score: r.score,
        label: r.label,
        explanation: r.explanation,
        evaluator_model: r.evaluator_model,
        evaluator_provider: r.evaluator_provider,
        evaluation_latency_ms: r.evaluation_latency_ms,
        evaluation_cost_usd: formatMicros(r.evaluation_cost_micros),
        job_created_at: isoTime(r.job_created_at),
        written_at: isoTime(r.written_at),
      }),
    ),
  };
  return json(body);
}
