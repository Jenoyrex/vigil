import { describe, expect, it } from "vitest";
import { quantile } from "../src/routes/analytics";
import { callJson, workspace } from "./helpers";

const T = (n: number) => n.toString(16).padStart(32, "0");
const S = (n: number) => n.toString(16).padStart(16, "0");
const HOUR = 3600e3;

async function seeded() {
  const ws = await workspace({ relevance: false });
  const base = Math.floor(Date.now() / HOUR) * HOUR - 2 * HOUR; // start of the hour two hours ago
  const at = (offset: number, duration: number) => ({ start_time: new Date(base + offset).toISOString(), end_time: new Date(base + offset + duration).toISOString() });
  const spans = [
    { trace_id: T(1), span_id: S(1), name: "a", environment: "prod", span_type: "llm", release: "v1", ...at(0, 100), llm_provider: "openai", llm_model: "gpt", llm_input_tokens: 10, llm_output_tokens: 5, llm_total_tokens: 15, llm_cost_usd: 0.001 },
    { trace_id: T(1), span_id: S(2), name: "b", environment: "prod", span_type: "tool", ...at(60_000, 200), status: "error" },
    { trace_id: T(2), span_id: S(3), name: "c", environment: "prod", span_type: "llm", ...at(HOUR + 1000, 300), llm_provider: "anthropic", llm_model: "claude", llm_input_tokens: 20, llm_output_tokens: 10, llm_total_tokens: 30, llm_cost_usd: 0.0025 },
    { trace_id: T(3), span_id: S(4), name: "d", environment: "staging", span_type: "llm", ...at(HOUR + 2000, 400), llm_provider: "openai", llm_cost_usd: 0.0005 },
  ];
  const res = await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans } });
  if (res.status !== 200) throw new Error(JSON.stringify(res.body));
  return { ws, base };
}

describe("quantile (ClickHouse quantile semantics)", () => {
  it("interpolates linearly and returns 0 for no data", () => {
    expect(quantile([], 0.5)).toBe(0);
    expect(quantile([100], 0.99)).toBe(100);
    expect(quantile([100, 200, 300, 400], 0.5)).toBe(250);
    expect(quantile([100, 200, 300, 400], 0.9)).toBeCloseTo(370, 10);
    expect(quantile([100, 200, 300, 400], 0.99)).toBeCloseTo(397, 10);
  });
});

describe("GET /v1/analytics/spans", () => {
  it("returns flat metrics with the SpanAnalyticsResponse contract", async () => {
    const { ws } = await seeded();
    const res = await callJson("GET", "/v1/analytics/spans", { session: ws.session, project: ws.projectId });
    expect(res.status).toBe(200);
    expect(Object.keys(res.body).sort()).toEqual([
      "bucket", "buckets", "error_rate", "error_span_count", "group_by", "groups", "latency_ms", "span_count", "start_time_from", "start_time_to",
    ]);
    expect(res.body).toMatchObject({ group_by: null, bucket: null, groups: null, buckets: null, span_count: 4, error_span_count: 1, error_rate: 0.25 });
    expect(res.body.latency_ms).toEqual({ p50: 250, p90: quantile([100, 200, 300, 400], 0.9), p99: quantile([100, 200, 300, 400], 0.99) });
    expect(Date.parse(res.body.start_time_to) - Date.parse(res.body.start_time_from)).toBe(24 * HOUR);
  });

  it("groups by a column (count desc) and filters", async () => {
    const { ws } = await seeded();
    const res = await callJson("GET", "/v1/analytics/spans?group_by=environment", { apiKey: ws.apiKey });
    expect(res.body).toMatchObject({ group_by: "environment", span_count: null, latency_ms: null, buckets: null });
    expect(res.body.groups.map((g: any) => [g.value, g.span_count, g.error_span_count, g.error_rate])).toEqual([
      ["prod", 3, 1, 1 / 3],
      ["staging", 1, 0, 0],
    ]);
    expect(Object.keys(res.body.groups[0]).sort()).toEqual(["error_rate", "error_span_count", "latency_ms", "span_count", "value"]);
    const release = await callJson("GET", "/v1/analytics/spans?group_by=release", { apiKey: ws.apiKey });
    expect(release.body.groups.map((g: any) => [g.value, g.span_count])).toEqual([["", 3], ["v1", 1]]);
    const llmOnly = await callJson("GET", "/v1/analytics/spans?span_type=llm&environment=prod", { apiKey: ws.apiKey });
    expect(llmOnly.body.span_count).toBe(2);
  });

  it("buckets by hour in UTC, ascending", async () => {
    const { ws, base } = await seeded();
    const res = await callJson("GET", "/v1/analytics/spans?bucket=hour", { apiKey: ws.apiKey });
    expect(res.body.buckets.map((b: any) => [b.bucket_start, b.span_count, b.error_span_count])).toEqual([
      [new Date(base).toISOString().replace(".000Z", "Z"), 2, 1],
      [new Date(base + HOUR).toISOString().replace(".000Z", "Z"), 2, 0],
    ]);
    expect(res.body.buckets[1].latency_ms).toEqual({ p50: 350, p90: 390, p99: 399 });
  });

  it("validates like apps/api", async () => {
    const { ws } = await seeded();
    const q = (s: string) => callJson("GET", `/v1/analytics/spans?${s}`, { apiKey: ws.apiKey });
    expect(await q("group_by=environment&bucket=hour")).toMatchObject({ status: 422, body: { detail: "group_by and bucket are mutually exclusive." } });
    expect((await q("group_by=model")).body.detail[0].msg).toBe("Input should be 'environment', 'span_type', 'release' or 'resource'");
    expect((await q("bucket=week")).body.detail[0].msg).toBe("Input should be 'hour' or 'day'");
    // Query validation precedes the service-level exclusivity check.
    expect((await q("group_by=environment&bucket=hour&start_time_to=bad")).body.detail[0].loc).toEqual(["query", "start_time_to"]);
  });

  it("returns zeros for an empty window", async () => {
    const { ws } = await seeded();
    const res = await callJson("GET", "/v1/analytics/spans?start_time_from=2020-01-01T00:00:00Z&start_time_to=2020-01-02T00:00:00Z", { apiKey: ws.apiKey });
    expect(res.body).toMatchObject({ span_count: 0, error_span_count: 0, error_rate: 0, latency_ms: { p50: 0, p90: 0, p99: 0 } });
  });
});

describe("GET /v1/analytics/llm-usage", () => {
  it("sums LLM spans (llm_provider IS NOT NULL) with string costs", async () => {
    const { ws } = await seeded();
    const res = await callJson("GET", "/v1/analytics/llm-usage", { session: ws.session, project: ws.projectId });
    expect(res.status).toBe(200);
    expect(Object.keys(res.body).sort()).toEqual([
      "group_by", "groups", "llm_span_count", "start_time_from", "start_time_to", "total_cost_usd", "total_input_tokens", "total_output_tokens", "total_tokens",
    ]);
    expect(res.body).toMatchObject({ group_by: null, groups: null, llm_span_count: 3, total_input_tokens: 30, total_output_tokens: 15, total_tokens: 45, total_cost_usd: "0.004000" });
  });

  it("groups by provider ordered by cost", async () => {
    const { ws } = await seeded();
    const res = await callJson("GET", "/v1/analytics/llm-usage?group_by=llm_provider", { apiKey: ws.apiKey });
    expect(res.body.groups).toEqual([
      { value: "anthropic", llm_span_count: 1, total_input_tokens: 20, total_output_tokens: 10, total_tokens: 30, total_cost_usd: "0.002500" },
      { value: "openai", llm_span_count: 2, total_input_tokens: 10, total_output_tokens: 5, total_tokens: 15, total_cost_usd: "0.001500" },
    ]);
    const models = await callJson("GET", "/v1/analytics/llm-usage?group_by=llm_model&environment=staging", { apiKey: ws.apiKey });
    expect(models.body.groups).toEqual([{ value: "", llm_span_count: 1, total_input_tokens: 0, total_output_tokens: 0, total_tokens: 0, total_cost_usd: "0.000500" }]);
  });

  it("reports zero usage as 0 / \"0.000000\"", async () => {
    const ws = await workspace({ relevance: false });
    const res = await callJson("GET", "/v1/analytics/llm-usage", { apiKey: ws.apiKey });
    expect(res.body).toMatchObject({ llm_span_count: 0, total_tokens: 0, total_cost_usd: "0.000000" });
  });

  it("is scoped to the caller's project", async () => {
    await seeded();
    const other = await workspace({ relevance: false });
    expect((await callJson("GET", "/v1/analytics/spans", { apiKey: other.apiKey })).body.span_count).toBe(0);
  });
});
