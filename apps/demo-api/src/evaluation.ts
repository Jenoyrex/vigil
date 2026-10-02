// Synchronous evaluation during ingest, replacing production's poller +
// worker for the demo (ADR 009). Semantics follow production:
//   * only `span_type = 'llm'` spans are eligible (worker poller);
//   * a job exists only if the project's config is enabled and the span is
//     sampled in (apps/api services/evaluations.create_evaluation_job);
//   * one job per (span, evaluator, version) -- a re-sent span that already
//     has a job is not re-evaluated;
//   * the result row's evaluation_id is the job id (worker result_mapping).

import { type Env, MAX_EVALUATIONS_PER_REQUEST, MAX_EVALUATION_TEXT_BYTES_PER_REQUEST } from "./config";
import type { SpanRow } from "./ingest";
import { utf8Length } from "./pyjson";
import { EVALUATOR_NAME, EVALUATOR_VERSION, type EvaluationResult, InvalidThresholdError, evaluateRelevance } from "./relevance";
import { sha256Bytes } from "./security";

const DEMO_WORKER_ID = "demo-api-ingest";
const MAX_LAST_ERROR_LENGTH = 2000;

/** apps/api's `_is_sampled_in`: deterministic SHA-256 bucket. */
export async function isSampledIn(
  projectId: string,
  traceId: string,
  spanId: string,
  evaluatorName: string,
  samplingRate: number,
): Promise<boolean> {
  if (samplingRate <= 0) return false;
  if (samplingRate >= 1) return true;
  const digest = await sha256Bytes(`${projectId}:${traceId}:${spanId}:${evaluatorName}`);
  const bucket = Number(new DataView(digest.buffer).getBigUint64(0)) / 2 ** 64;
  return bucket < samplingRate;
}

interface Config {
  enabled: number;
  sampling_rate: number;
  threshold: number | null;
  max_retries: number;
}

interface JobRow {
  id: string;
  project_id: string;
  trace_id: string;
  span_id: string;
  evaluator_name: string;
  evaluator_version: string;
  status: "succeeded" | "dead_letter";
  attempt_count: number;
  max_retries: number;
  claimed_at: number | null;
  claimed_by: string | null;
  last_error: string | null;
  created_at: number;
  updated_at: number;
}

const JOB_COLUMNS = [
  "id", "project_id", "trace_id", "span_id", "evaluator_name", "evaluator_version", "status", "attempt_count",
  "max_retries", "claimed_at", "claimed_by", "last_error", "created_at", "updated_at",
];
const RESULT_COLUMNS = [
  "evaluation_id", "project_id", "trace_id", "span_id", "evaluator_name", "evaluator_version", "score", "label",
  "explanation", "evaluator_model", "evaluation_latency_ms", "job_created_at", "written_at",
];
const select = (cols: string[]) => cols.map((c) => `json_extract(j.value, '$.${c}')`).join(", ");

export async function evaluateIngestedSpans(env: Env, projectId: string, rows: SpanRow[]): Promise<void> {
  const llmSpans = rows.filter((r) => r.span_type === "llm");
  if (llmSpans.length === 0) return;
  const config = await env.DB.prepare(
    "SELECT enabled, sampling_rate, threshold, max_retries FROM evaluator_configs WHERE project_id = ? AND evaluator_name = ?",
  )
    .bind(projectId, EVALUATOR_NAME)
    .first<Config>();
  if (!config || !config.enabled) return;

  const sampled: SpanRow[] = [];
  for (const r of llmSpans) {
    if (await isSampledIn(projectId, r.trace_id, r.span_id, EVALUATOR_NAME, config.sampling_rate)) sampled.push(r);
  }
  if (sampled.length === 0) return;

  const { results: existing } = await env.DB.prepare(
    `SELECT trace_id, span_id FROM evaluation_jobs
      WHERE project_id = ? AND evaluator_name = ? AND evaluator_version = ?
        AND (trace_id, span_id) IN (SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') FROM json_each(?))`,
  )
    .bind(projectId, EVALUATOR_NAME, EVALUATOR_VERSION, JSON.stringify(sampled.map((r) => [r.trace_id, r.span_id])))
    .all<{ trace_id: string; span_id: string }>();
  const done = new Set(existing.map((e) => `${e.trace_id}:${e.span_id}`));

  const jobs: JobRow[] = [];
  const results: (EvaluationResult & { evaluation_id: string; project_id: string; trace_id: string; span_id: string; job_created_at: number; written_at: number })[] = [];
  let evaluated = 0;
  let textBytes = 0;
  for (const r of sampled) {
    if (done.has(`${r.trace_id}:${r.span_id}`)) continue;
    const now = Date.now();
    const job: JobRow = {
      id: crypto.randomUUID(),
      project_id: projectId,
      trace_id: r.trace_id,
      span_id: r.span_id,
      evaluator_name: EVALUATOR_NAME,
      evaluator_version: EVALUATOR_VERSION,
      status: "succeeded",
      attempt_count: 1,
      max_retries: config.max_retries,
      claimed_at: now,
      claimed_by: DEMO_WORKER_ID,
      last_error: null,
      created_at: now,
      updated_at: now,
    };
    jobs.push(job);

    const size = utf8Length(r.input) + utf8Length(r.output);
    if (evaluated >= MAX_EVALUATIONS_PER_REQUEST || textBytes + size > MAX_EVALUATION_TEXT_BYTES_PER_REQUEST) {
      Object.assign(job, {
        status: "dead_letter",
        attempt_count: 0,
        claimed_at: null,
        claimed_by: null,
        last_error:
          `Skipped by the public demo: at most ${MAX_EVALUATIONS_PER_REQUEST} spans / ` +
          `${MAX_EVALUATION_TEXT_BYTES_PER_REQUEST / 1024} KiB of input+output text are evaluated per ingest request.`,
      });
      continue;
    }
    evaluated += 1;
    textBytes += size;
    try {
      // services/worker adapters: span input/output text, "" when absent.
      const result = evaluateRelevance(r.input ?? "", r.output ?? "", config.threshold);
      results.push({ ...result, evaluation_id: job.id, project_id: projectId, trace_id: r.trace_id, span_id: r.span_id, job_created_at: job.created_at, written_at: Date.now() });
    } catch (error) {
      if (!(error instanceof InvalidThresholdError)) throw error;
      // Production retries this ValueError until max_retries; it can never
      // succeed, so the demo records the terminal state directly.
      job.status = "dead_letter";
      job.last_error = `ValueError: ${error.message}`.slice(0, MAX_LAST_ERROR_LENGTH);
    }
  }
  if (jobs.length === 0) return;

  await env.DB.batch([
    env.DB.prepare(
      `INSERT OR IGNORE INTO evaluation_jobs (${JOB_COLUMNS.join(", ")}) SELECT ${select(JOB_COLUMNS)} FROM json_each(?) j`,
    ).bind(JSON.stringify(jobs)),
    env.DB.prepare(
      `INSERT OR IGNORE INTO evaluation_results (${RESULT_COLUMNS.join(", ")}) SELECT ${select(RESULT_COLUMNS)} FROM json_each(?) j
        WHERE EXISTS (SELECT 1 FROM evaluation_jobs e WHERE e.id = json_extract(j.value, '$.evaluation_id'))`,
    ).bind(JSON.stringify(results)),
  ]);
}
