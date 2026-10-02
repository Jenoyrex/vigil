import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { parseJsonObject } from "../src/body";
import { ApiError, isoTime } from "../src/http";
import { costMicros, transformRequest, validateTracesRequest } from "../src/ingest";
import { formatMicros } from "../src/routes/traces";
import ingestParity from "./fixtures/ingest-parity.json";
import { ISO_UTC, call, callJson, signup, workspace } from "./helpers";

type FixtureRow = Record<string, any>;

function transformFixture(body: string) {
  return transformRequest(validateTracesRequest(parseJsonObject(body)));
}

function comparable(row: ReturnType<typeof transformFixture>[number]): FixtureRow {
  return {
    trace_id: row.trace_id,
    span_id: row.span_id,
    parent_span_id: row.parent_span_id,
    name: row.name,
    span_type: row.span_type,
    resource: row.resource,
    start_time: row.start_time,
    end_time: row.end_time,
    status: row.status,
    status_message: row.status_message,
    input: row.input,
    input_size_bytes: row.input_size_bytes,
    input_truncated: row.input_truncated,
    output: row.output,
    output_size_bytes: row.output_size_bytes,
    output_truncated: row.output_truncated,
    attributes: Object.fromEntries(row.attributes),
    attributes_truncated: row.attributes_truncated,
    "events.time": row.events.map((e) => e.time),
    "events.name": row.events.map((e) => e.name),
    "events.attributes": row.events.map((e) => Object.fromEntries(e.attributes)),
    events_truncated: row.events_truncated,
    llm_provider: row.llm_provider,
    llm_model: row.llm_model,
    llm_input_tokens: row.llm_input_tokens,
    llm_output_tokens: row.llm_output_tokens,
    llm_total_tokens: row.llm_total_tokens,
    llm_cost_micros: row.llm_cost_micros,
    environment: row.environment,
    release: row.release,
  };
}

describe("ingest parity with apps/api (fixtures from the production transform)", () => {
  ingestParity.cases.forEach((c: any, i: number) => {
    it(`case ${i}: ${c.errors ? "422" : c.production_server_error ? "production 500" : "accepted"}`, () => {
      if (c.errors) {
        let error: unknown;
        try {
          transformFixture(c.body);
        } catch (e) {
          error = e;
        }
        expect(error).toBeInstanceOf(ApiError);
        expect((error as ApiError).status).toBe(422);
        expect(((error as ApiError).detail as any[]).map(({ type, loc, msg }) => ({ type, loc, msg }))).toEqual(c.errors);
      } else if (c.production_server_error) {
        // Mixed naive/aware timestamps: production raises TypeError (HTTP 500);
        // the demo treats naive as UTC (as ClickHouse storage does) and accepts.
        expect(transformFixture(c.body)).toHaveLength(1);
      } else {
        expect(transformFixture(c.body).map(comparable)).toEqual(c.rows);
      }
    });
  });

  it("truncates to the Decimal64(6) cost representation", () => {
    expect(costMicros(0.0012345678)).toBe(1234);
    expect(costMicros(2)).toBe(2_000_000);
    expect(costMicros(1e-7)).toBe(0);
    expect(formatMicros(1234)).toBe("0.001234");
    expect(formatMicros(0)).toBe("0.000000");
    expect(formatMicros(2_500_000)).toBe("2.500000");
  });
});

const T = (n: number) => n.toString(16).padStart(32, "0");
const S = (n: number) => n.toString(16).padStart(16, "0");

function span(overrides: Record<string, unknown> = {}) {
  const now = Date.now();
  return {
    trace_id: T(1),
    span_id: S(1),
    name: "root",
    start_time: new Date(now - 1000).toISOString(),
    end_time: new Date(now - 500).toISOString(),
    ...overrides,
  };
}

describe("POST /v1/traces", () => {
  it("authenticates with the project API key and reports accepted spans", async () => {
    const ws = await workspace({ relevance: false });
    const res = await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [span(), span({ span_id: S(2) })] } });
    expect(res.status).toBe(200);
    expect(res.body.accepted).toBe(2);
    expect(res.body.request_id).toMatch(/^[0-9a-f]{32}$/);
    const keys = await callJson("GET", `/v1/projects/${ws.projectId}/api-keys`, { session: ws.session });
    expect(keys.body.items[0].last_used_at).toMatch(ISO_UTC);
  });

  it("rejects missing, malformed, unknown and revoked keys like apps/api", async () => {
    const ws = await workspace({ relevance: false });
    const body = { spans: [span()] };
    for (const apiKey of [undefined, "not-a-key", "vgl_abc.unknown"]) {
      const res = await callJson("POST", "/v1/traces", { apiKey, body });
      expect(res).toMatchObject({ status: 401, body: { detail: "Invalid or missing API key." } });
      expect(res.response.headers.get("www-authenticate")).toBe("Bearer");
    }
    const keys = await callJson("GET", `/v1/projects/${ws.projectId}/api-keys`, { session: ws.session });
    await call("POST", `/v1/projects/${ws.projectId}/api-keys/${keys.body.items[0].id}/revoke`, { session: ws.session });
    expect(await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body })).toMatchObject({
      status: 401,
      body: { detail: "This API key has been revoked." },
    });
  });

  it("returns 413 for an oversized body before authenticating", async () => {
    const res = await callJson("POST", "/v1/traces", { rawBody: "x".repeat(256 * 1024 + 1) });
    expect(res).toMatchObject({ status: 413, body: { detail: "Request body exceeds the maximum allowed size of 262144 bytes." } });
  });

  it("validates after authenticating (422 with pydantic-shaped detail)", async () => {
    const ws = await workspace({ relevance: false });
    const res = await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [] } });
    expect(res.status).toBe(422);
    expect(res.body.detail).toEqual([{ type: "too_short", loc: ["body", "spans"], msg: "List should have at least 1 item after validation, not 0" }]);
  });

  it("replaces a re-sent span instead of duplicating it (ReplacingMergeTree semantics)", async () => {
    const ws = await workspace({ relevance: false });
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [span({ name: "first" })] } });
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [span({ name: "second" }), span({ name: "third" })] } });
    const trace = await callJson("GET", `/v1/traces/${T(1)}`, { apiKey: ws.apiKey });
    expect(trace.body.total_span_count).toBe(1);
    expect(trace.body.spans[0].name).toBe("third");
  });

  it("enforces the per-project span cap with a clear 403", async () => {
    const ws = await workspace({ relevance: false });
    const batch = (offset: number) => ({ spans: Array.from({ length: 1000 }, (_, i) => span({ trace_id: T(offset + i), span_id: S(1), name: "s" })) });
    expect((await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: batch(0) })).status).toBe(200);
    expect((await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: batch(1000) })).status).toBe(200);
    // Re-sending stored spans is still allowed at the cap (replacement, not growth).
    expect((await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [span({ trace_id: T(5), name: "again" })] } })).status).toBe(200);
    const over = await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { spans: [span({ trace_id: T(99999) })] } });
    expect(over.status).toBe(403);
    expect(over.body.detail).toMatch(/^Public demo limit reached: a project stores at most 2000 spans \(this project has 2000\)/);
  });
});

describe("trace queries", () => {
  async function seeded() {
    const ws = await workspace({ relevance: false });
    const now = Date.now();
    const t = (ms: number) => new Date(now - ms).toISOString();
    const spans = [
      // trace 1: ok root + child, prod
      { trace_id: T(1), span_id: S(1), name: "root-1", start_time: t(60_000), end_time: t(50_000), status: "ok", environment: "prod" },
      { trace_id: T(1), span_id: S(2), parent_span_id: S(1), name: "child", start_time: t(58_000), end_time: t(55_000), llm_provider: "openai", llm_cost_usd: 0.0015, span_type: "llm", input: "q", output: "a" },
      // trace 2: error, staging
      { trace_id: T(2), span_id: S(3), name: "root-2", start_time: t(40_000), end_time: t(39_000), status: "error", environment: "staging" },
      // trace 3: no root span -> "unknown"
      { trace_id: T(3), span_id: S(4), parent_span_id: S(9), name: "orphan", start_time: t(20_000), end_time: t(19_000), environment: "prod" },
    ];
    await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body: { resource: { "service.name": "checkout" }, spans } });
    return ws;
  }

  it("lists traces newest first with the TraceSummary contract", async () => {
    const ws = await seeded();
    const res = await callJson("GET", "/v1/traces", { session: ws.session, project: ws.projectId });
    expect(res.status).toBe(200);
    expect(Object.keys(res.body).sort()).toEqual(["next_cursor", "traces"]);
    expect(res.body.next_cursor).toBeNull();
    expect(res.body.traces.map((t: any) => [t.trace_id, t.status, t.span_count, t.error_span_count, t.root_span_name])).toEqual([
      [T(3), "unknown", 1, 0, null],
      [T(2), "error", 1, 1, "root-2"],
      [T(1), "ok", 2, 0, "root-1"],
    ]);
    const first = res.body.traces[2];
    expect(Object.keys(first).sort()).toEqual(
      ["duration_ms", "end_time", "environment", "error_span_count", "resource", "root_span_name", "span_count", "start_time", "status", "trace_id"],
    );
    expect(first.duration_ms).toBe(10_000);
    expect(first.resource).toBe("checkout");
    expect(first.start_time).toMatch(ISO_UTC);
  });

  it("filters by environment, resource and has_error", async () => {
    const ws = await seeded();
    const ids = async (q: string) =>
      (await callJson("GET", `/v1/traces?${q}`, { apiKey: ws.apiKey })).body.traces.map((t: any) => t.trace_id);
    expect(await ids("has_error=true")).toEqual([T(2)]);
    expect(await ids("has_error=no")).toEqual([T(3), T(1)]);
    expect(await ids("environment=staging")).toEqual([T(2)]);
    expect(await ids("resource=nope")).toEqual([]);
  });

  it("paginates with an opaque, stable cursor", async () => {
    const ws = await seeded();
    const page1 = await callJson("GET", "/v1/traces?limit=2", { apiKey: ws.apiKey });
    expect(page1.body.traces.map((t: any) => t.trace_id)).toEqual([T(3), T(2)]);
    expect(page1.body.next_cursor).toEqual(expect.any(String));
    const decoded = JSON.parse(atob(page1.body.next_cursor.replace(/-/g, "+").replace(/_/g, "/")));
    expect(Object.keys(decoded)).toEqual(["start_time", "trace_id"]);
    expect(decoded.start_time).toMatch(/\+00:00$/);
    const page2 = await callJson("GET", `/v1/traces?limit=2&cursor=${encodeURIComponent(page1.body.next_cursor)}`, { apiKey: ws.apiKey });
    expect(page2.body.traces.map((t: any) => t.trace_id)).toEqual([T(1)]);
    expect(page2.body.next_cursor).toBeNull();
  });

  it("validates query parameters and the time window like apps/api", async () => {
    const ws = await seeded();
    const q = async (s: string) => callJson("GET", `/v1/traces?${s}`, { apiKey: ws.apiKey });
    expect((await q("limit=0")).body.detail[0]).toMatchObject({ type: "greater_than_equal", loc: ["query", "limit"] });
    expect((await q("limit=101")).body.detail[0]).toMatchObject({ type: "less_than_equal", loc: ["query", "limit"] });
    expect((await q("has_error=maybe")).body.detail[0].type).toBe("bool_parsing");
    expect((await q("start_time_from=2026-09-01T00:00:00")).body.detail[0].msg).toBe(
      "Value error, must be a timezone-aware RFC3339 timestamp (include a UTC offset)",
    );
    expect(await q("start_time_from=2026-09-01T00:00:00Z&start_time_to=2026-08-01T00:00:00Z")).toMatchObject({
      status: 422,
      body: { detail: "start_time_from must not be after start_time_to." },
    });
    expect(await q("start_time_from=2026-08-01T00:00:00Z&start_time_to=2026-09-01T00:00:00Z")).toMatchObject({
      status: 422,
      body: { detail: "Time window must not exceed 7 days; narrow start_time_from/start_time_to." },
    });
    expect(await q("cursor=garbage")).toMatchObject({ status: 422, body: { detail: "Malformed pagination cursor." } });
  });

  it("returns a trace with ordered spans and the SpanOut contract", async () => {
    const ws = await seeded();
    const res = await callJson("GET", `/v1/traces/${T(1).toUpperCase()}`, { session: ws.session, project: ws.projectId });
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ trace_id: T(1), status: "ok", span_count: 2, total_span_count: 2, truncated: false, duration_ms: 10_000 });
    expect(res.body.spans.map((s: any) => s.span_id)).toEqual([S(1), S(2)]);
    const child = res.body.spans[1];
    expect(Object.keys(child).sort()).toEqual([
      "attributes", "attributes_truncated", "duration_ms", "end_time", "environment", "events", "events_truncated", "input",
      "input_size_bytes", "input_truncated", "llm_cost_usd", "llm_input_tokens", "llm_model", "llm_output_tokens", "llm_provider",
      "llm_total_tokens", "name", "output", "output_size_bytes", "output_truncated", "parent_span_id", "release", "resource",
      "span_id", "span_type", "start_time", "status", "status_message",
    ]);
    expect(child).toMatchObject({ parent_span_id: S(1), llm_cost_usd: "0.001500", environment: "unknown", status: "unset", input: "q" });

    const single = await callJson("GET", `/v1/traces/${T(1)}/spans/${S(2)}`, { apiKey: ws.apiKey });
    expect(single.body).toEqual(child);
  });

  it("honors the start_date hint and 404/422 contracts", async () => {
    const ws = await seeded();
    const today = isoTime(Date.now() - 60_000).slice(0, 10);
    expect((await callJson("GET", `/v1/traces/${T(1)}?start_date=${today}`, { apiKey: ws.apiKey })).status).toBe(200);
    expect(await callJson("GET", `/v1/traces/${T(1)}?start_date=2001-01-01`, { apiKey: ws.apiKey })).toMatchObject({ status: 404, body: { detail: "Trace not found." } });
    expect(await callJson("GET", `/v1/traces/${T(1)}/spans/${S(7)}`, { apiKey: ws.apiKey })).toMatchObject({ status: 404, body: { detail: "Span not found." } });
    const bad = await callJson("GET", "/v1/traces/xyz", { apiKey: ws.apiKey });
    expect(bad.body.detail).toEqual([{ type: "value_error", loc: ["path", "trace_id"], msg: "Value error, trace_id must be exactly 32 hexadecimal characters" }]);
  });

  it("isolates projects for both API keys and dashboard sessions", async () => {
    const a = await seeded();
    const b = await workspace({ relevance: false });
    expect((await callJson("GET", `/v1/traces/${T(1)}`, { apiKey: b.apiKey })).status).toBe(404);
    expect((await callJson("GET", "/v1/traces", { apiKey: b.apiKey })).body.traces).toEqual([]);
    // B's session naming A's project is indistinguishable from a missing project.
    expect(await callJson("GET", "/v1/traces", { session: b.session, project: a.projectId })).toMatchObject({ status: 404, body: { detail: "Project not found." } });
    expect(await callJson("GET", "/v1/traces", { session: b.session })).toMatchObject({ status: 404, body: { detail: "Project not found." } });
    expect(await callJson("GET", "/v1/traces")).toMatchObject({ status: 401, body: { detail: "Invalid or missing API key." } });
    const { session } = await signup();
    await callJson("POST", "/v1/auth/logout", { session });
    expect(await callJson("GET", "/v1/traces", { session, project: a.projectId })).toMatchObject({ status: 401, body: { detail: "Invalid or expired session." } });
  });

  it("stores spans in D1 scoped to the key's project", async () => {
    const ws = await seeded();
    const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM spans WHERE project_id = ?").bind(ws.projectId).first<{ n: number }>();
    expect(row?.n).toBe(4);
  });
});
