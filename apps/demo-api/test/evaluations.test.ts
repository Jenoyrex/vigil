import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { isSampledIn } from "../src/evaluation";
import { EVALUATOR_VERSION, evaluateRelevance, pyFixed } from "../src/relevance";
import { ENGLISH_STOP_WORDS } from "../src/stopWords";
import relevanceParity from "./fixtures/relevance-parity.json";
import samplingParity from "./fixtures/sampling-parity.json";
import stopWords from "./fixtures/english-stop-words.json";
import { ISO_UTC, UUID, callJson, workspace } from "./helpers";

describe("TF-IDF relevance parity with services/evaluator (Python-produced scores)", () => {
  it("uses scikit-learn's exact English stop-word list", () => {
    expect([...ENGLISH_STOP_WORDS].sort()).toEqual(stopWords);
  });

  relevanceParity.cases.forEach((c: any, i: number) => {
    it(`case ${i}: ${JSON.stringify(c.input).slice(0, 40)} -> ${c.label}`, () => {
      const result = evaluateRelevance(c.input, c.output, c.threshold);
      expect(result.label).toBe(c.label);
      expect(result.explanation).toBe(c.explanation);
      expect(result.evaluator_name).toBe(c.evaluator_name);
      expect(result.evaluator_version).toBe(c.evaluator_version);
      expect(result.evaluator_model).toBe(c.evaluator_model);
      if (c.score === null) expect(result.score).toBeNull();
      else expect(Math.abs((result.score as number) - c.score)).toBeLessThanOrEqual(1e-12);
    });
  });

  it("formats like Python's '.4f' (round half to even on the exact binary value)", () => {
    expect(pyFixed(0.03125, 4)).toBe("0.0312"); // exact tie -> even
    expect(pyFixed(0.03135, 4)).toBe("0.0314"); // binary value is just above the tie (verified in Python)
    expect(pyFixed(0.5, 4)).toBe("0.5000");
    expect(pyFixed(1, 4)).toBe("1.0000");
    expect(pyFixed(0, 4)).toBe("0.0000");
    expect(pyFixed(-0.00001, 4)).toBe("-0.0000");
    expect(pyFixed(0.99999, 4)).toBe("1.0000");
  });

  it("raises the evaluator's ValueError message for an out-of-range threshold", () => {
    expect(() => evaluateRelevance("a b", "a b", 2)).toThrow("threshold must be within [0.0, 1.0], got 2.0.");
  });
});

describe("deterministic sampling parity with apps/api _is_sampled_in", () => {
  it("matches all 1000 production decisions", async () => {
    for (const c of samplingParity.cases) {
      expect(await isSampledIn(c.project_id, c.trace_id, c.span_id, c.evaluator_name, c.sampling_rate)).toBe(c.sampled);
    }
  });
});

const T = (n: number) => n.toString(16).padStart(32, "0");
const S = (n: number) => n.toString(16).padStart(16, "0");

function llmSpan(n: number, overrides: Record<string, unknown> = {}) {
  const now = Date.now();
  return {
    trace_id: T(n),
    span_id: S(n),
    name: "chat",
    span_type: "llm",
    start_time: new Date(now - 2000).toISOString(),
    end_time: new Date(now - 1000).toISOString(),
    input: "What is the capital of France?",
    output: "The capital of France is Paris.",
    ...overrides,
  };
}

describe("evaluation during ingest", () => {
  it("evaluates an eligible LLM span synchronously: succeeded job + result", async () => {
    const ws = await workspace();
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [llmSpan(1)] } });

    const jobs = await callJson("GET", "/v1/evaluations/jobs", { session: ws.session, project: ws.projectId });
    expect(jobs.status).toBe(200);
    expect(jobs.body.next_cursor).toBeNull();
    expect(jobs.body.jobs).toHaveLength(1);
    const job = jobs.body.jobs[0];
    expect(Object.keys(job).sort()).toEqual([
      "attempt_count", "claimed_at", "claimed_by", "created_at", "evaluator_name", "evaluator_version", "id", "last_error",
      "max_retries", "next_attempt_at", "span_id", "status", "trace_id", "updated_at",
    ]);
    expect(job).toMatchObject({
      trace_id: T(1), span_id: S(1), evaluator_name: "relevance", evaluator_version: EVALUATOR_VERSION,
      status: "succeeded", attempt_count: 1, max_retries: 3, next_attempt_at: null, claimed_by: "demo-api-ingest", last_error: null,
    });
    expect(job.id).toMatch(UUID);
    expect(job.claimed_at).toMatch(ISO_UTC);

    const results = await callJson("GET", `/v1/traces/${T(1)}/spans/${S(1)}/evaluations`, { session: ws.session, project: ws.projectId });
    expect(results.status).toBe(200);
    expect(results.body.results).toHaveLength(1);
    const r = results.body.results[0];
    expect(Object.keys(r).sort()).toEqual([
      "evaluation_cost_usd", "evaluation_id", "evaluation_latency_ms", "evaluator_model", "evaluator_name", "evaluator_provider",
      "evaluator_version", "explanation", "job_created_at", "label", "score", "span_id", "trace_id", "written_at",
    ]);
    const expected = relevanceParity.cases[1];
    expect(r).toMatchObject({
      evaluation_id: job.id, evaluator_name: "relevance", evaluator_model: "tfidf-cosine", evaluator_provider: null,
      evaluation_cost_usd: null, label: expected.label, explanation: expected.explanation, job_created_at: job.created_at,
    });
    expect(Math.abs(r.score - (expected.score as number))).toBeLessThanOrEqual(1e-12);
    expect(typeof r.evaluation_latency_ms).toBe("number");
  });

  it("only evaluates llm spans, only when enabled, and respects sampling_rate", async () => {
    const off = await workspace({ relevance: false });
    await callJson("POST", "/v1/traces", { apiKey: off.apiKey, body: { spans: [llmSpan(1)] } });
    expect((await callJson("GET", "/v1/evaluations/jobs", { apiKey: off.apiKey })).body.jobs).toEqual([]);

    const ws = await workspace();
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [llmSpan(1, { span_type: "tool" })] } });
    expect((await callJson("GET", "/v1/evaluations/jobs", { apiKey: ws.apiKey })).body.jobs).toEqual([]);

    await callJson("PUT", "/v1/evaluations/configs/relevance", { apiKey: ws.apiKey, body: { enabled: true, sampling_rate: 0 } });
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [llmSpan(2)] } });
    expect((await callJson("GET", "/v1/evaluations/jobs", { apiKey: ws.apiKey })).body.jobs).toEqual([]);
  });

  it("does not re-evaluate a re-sent span (one job per span/evaluator/version)", async () => {
    const ws = await workspace();
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [llmSpan(1)] } });
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [llmSpan(1, { output: "Bananas." })] } });
    const jobs = await callJson("GET", "/v1/evaluations/jobs", { apiKey: ws.apiKey });
    expect(jobs.body.jobs).toHaveLength(1);
    const results = await callJson("GET", `/v1/traces/${T(1)}/spans/${S(1)}/evaluations`, { apiKey: ws.apiKey });
    expect(results.body.results).toHaveLength(1);
  });

  it("uses the configured threshold and records an invalid one as dead_letter", async () => {
    const ws = await workspace();
    await callJson("PUT", "/v1/evaluations/configs/relevance", { apiKey: ws.apiKey, body: { enabled: true, sampling_rate: 1, threshold: 0.9 } });
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [llmSpan(1)] } });
    const r = await callJson("GET", `/v1/traces/${T(1)}/spans/${S(1)}/evaluations`, { apiKey: ws.apiKey });
    expect(r.body.results[0].label).toBe("not_relevant");
    expect(r.body.results[0].explanation).toContain("threshold=0.9000");

    await callJson("PUT", "/v1/evaluations/configs/relevance", { apiKey: ws.apiKey, body: { enabled: true, sampling_rate: 1, threshold: 2 } });
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [llmSpan(2)] } });
    const jobs = await callJson("GET", "/v1/evaluations/jobs?status=dead_letter", { apiKey: ws.apiKey });
    expect(jobs.body.jobs).toHaveLength(1);
    expect(jobs.body.jobs[0].last_error).toBe("ValueError: threshold must be within [0.0, 1.0], got 2.0.");
    expect((await callJson("GET", `/v1/traces/${T(2)}/spans/${S(2)}/evaluations`, { apiKey: ws.apiKey })).body.results).toEqual([]);
  });

  it("records spans beyond the per-request evaluation budget as dead_letter with a reason", async () => {
    const ws = await workspace();
    const spans = Array.from({ length: 27 }, (_, i) => llmSpan(i + 1));
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans } });
    const succeeded = await callJson("GET", "/v1/evaluations/jobs?status=succeeded&limit=100", { apiKey: ws.apiKey });
    const skipped = await callJson("GET", "/v1/evaluations/jobs?status=dead_letter&limit=100", { apiKey: ws.apiKey });
    expect(succeeded.body.jobs).toHaveLength(25);
    expect(skipped.body.jobs).toHaveLength(2);
    expect(skipped.body.jobs[0]).toMatchObject({ attempt_count: 0, claimed_at: null });
    expect(skipped.body.jobs[0].last_error).toMatch(/^Skipped by the public demo: at most 25 spans/);
  });
});

describe("evaluator configs and jobs endpoints", () => {
  it("upserts, reads and lists configs with the EvaluatorConfigOut contract", async () => {
    const ws = await workspace({ relevance: false });
    const auth = { session: ws.session, project: ws.projectId };
    expect(await callJson("GET", "/v1/evaluations/configs/relevance", auth)).toMatchObject({
      status: 404,
      body: { detail: "This evaluator has never been configured for this project." },
    });
    const put = await callJson("PUT", "/v1/evaluations/configs/relevance", { ...auth, body: { enabled: true } });
    expect(put.status).toBe(200);
    expect(put.body).toMatchObject({ evaluator_name: "relevance", enabled: true, sampling_rate: 0.1, threshold: null, max_retries: 3 });
    expect(Object.keys(put.body).sort()).toEqual(["created_at", "enabled", "evaluator_name", "max_retries", "sampling_rate", "threshold", "updated_at"]);
    const again = await callJson("PUT", "/v1/evaluations/configs/relevance", { ...auth, body: { enabled: false, sampling_rate: 0.5, threshold: 0.3, max_retries: 0 } });
    expect(again.body).toMatchObject({ enabled: false, sampling_rate: 0.5, threshold: 0.3, max_retries: 0, created_at: put.body.created_at });
    // Other names are accepted as in production (no job ever targets them).
    await callJson("PUT", "/v1/evaluations/configs/custom.v2", { ...auth, body: { enabled: true } });
    const list = await callJson("GET", "/v1/evaluations/configs", auth);
    expect(list.body.configs.map((c: any) => c.evaluator_name)).toEqual(["custom.v2", "relevance"]);
  });

  it("explicitly rejects relevance_embedding in the demo", async () => {
    const ws = await workspace({ relevance: false });
    const res = await callJson("PUT", "/v1/evaluations/configs/relevance_embedding", { apiKey: ws.apiKey, body: { enabled: true } });
    expect(res.status).toBe(422);
    expect(res.body.detail).toMatch(/^relevance_embedding \(BGE embedding relevance\) is not available in the public demo/);
    expect((await callJson("GET", "/v1/evaluations/configs", { apiKey: ws.apiKey })).body.configs).toEqual([]);
  });

  it("validates config bodies and names like apps/api", async () => {
    const ws = await workspace({ relevance: false });
    const put = (name: string, body: unknown) => callJson("PUT", `/v1/evaluations/configs/${name}`, { apiKey: ws.apiKey, body });
    expect((await put("relevance", {})).body.detail).toEqual([{ type: "missing", loc: ["body", "enabled"], msg: "Field required" }]);
    expect((await put("relevance", { enabled: true, sampling_rate: 2 })).body.detail[0]).toMatchObject({ type: "less_than_equal", loc: ["body", "sampling_rate"] });
    expect((await put("relevance", { enabled: "maybe" })).body.detail[0].type).toBe("bool_parsing");
    expect((await put("bad name!", { enabled: true })).body.detail[0]).toMatchObject({ type: "value_error", loc: ["path", "evaluator_name"] });
  });

  it("filters and paginates jobs newest first", async () => {
    const ws = await workspace();
    for (let i = 1; i <= 3; i++) await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [llmSpan(i)] } });
    const page1 = await callJson("GET", "/v1/evaluations/jobs?limit=2", { apiKey: ws.apiKey });
    expect(page1.body.jobs.map((j: any) => j.trace_id)).toEqual([T(3), T(2)]);
    const page2 = await callJson("GET", `/v1/evaluations/jobs?limit=2&cursor=${encodeURIComponent(page1.body.next_cursor)}`, { apiKey: ws.apiKey });
    expect(page2.body.jobs.map((j: any) => j.trace_id)).toEqual([T(1)]);
    expect(page2.body.next_cursor).toBeNull();
    expect((await callJson("GET", "/v1/evaluations/jobs?evaluator_name=other", { apiKey: ws.apiKey })).body.jobs).toEqual([]);
    const bad = await callJson("GET", "/v1/evaluations/jobs?status=done", { apiKey: ws.apiKey });
    expect(bad.body.detail[0].msg).toBe("Input should be 'pending', 'running', 'succeeded', 'failed' or 'dead_letter'");
    expect(await callJson("GET", "/v1/evaluations/jobs?cursor=x", { apiKey: ws.apiKey })).toMatchObject({ status: 422, body: { detail: "Malformed pagination cursor." } });
  });

  it("isolates evaluation data between projects", async () => {
    const a = await workspace();
    const b = await workspace();
    await callJson("POST", "/v1/traces", { apiKey: a.apiKey, body: { spans: [llmSpan(1)] } });
    expect((await callJson("GET", "/v1/evaluations/jobs", { apiKey: b.apiKey })).body.jobs).toEqual([]);
    expect((await callJson("GET", `/v1/traces/${T(1)}/spans/${S(1)}/evaluations`, { apiKey: b.apiKey })).body.results).toEqual([]);
    expect((await callJson("GET", "/v1/evaluations/configs", { session: b.session, project: a.projectId })).status).toBe(404);
    const rows = await env.DB.prepare("SELECT COUNT(*) AS n FROM evaluation_results WHERE project_id = ?").bind(a.projectId).first<{ n: number }>();
    expect(rows?.n).toBe(1);
  });
});
