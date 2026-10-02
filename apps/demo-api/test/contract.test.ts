// Demo API contract suite, derived from the dashboard's own API types
// (apps/dashboard/lib/api/types.ts and workspaceTypes.ts). The key lists below
// are checked against those interfaces at compile time (`npm run typecheck`
// fails if the dashboard adds, removes or renames a field), and against live
// responses at run time. The journey follows the dashboard BFF's call
// sequence for the eight Live Demo capabilities.

import { describe, expect, it } from "vitest";
import type {
  EvaluationJobListResponse,
  EvaluationJobOut,
  EvaluationResultOut,
  EvaluatorConfigListResponse,
  EvaluatorConfigOut,
  LlmUsageResponse,
  SpanAnalyticsBucket,
  SpanAnalyticsResponse,
  SpanEvaluationsResponse,
  SpanOut,
  TraceDetailResponse,
  TraceListResponse,
  TraceSummary,
} from "../../dashboard/lib/api/types";
import type { ApiKeyCreated, ApiKeyList, ApiKeyOut, Me, Organization, Project } from "../../dashboard/lib/api/workspaceTypes";
import { PASSWORD, callJson, unique } from "./helpers";

type Exact<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;
const keys = <T>() => <const K extends readonly (keyof T & string)[]>(k: K & (Exact<K[number], keyof T & string> extends true ? unknown : never)) => k;

const CONTRACT = {
  Project: keys<Project>()(["id", "name", "slug", "created_at"]),
  Organization: keys<Organization>()(["id", "name", "slug", "role", "projects"]),
  Me: keys<Me>()(["user", "organizations"]),
  ApiKeyOut: keys<ApiKeyOut>()(["id", "name", "key_prefix", "status", "created_at", "last_used_at", "revoked_at"]),
  ApiKeyCreated: keys<ApiKeyCreated>()(["id", "name", "key_prefix", "status", "created_at", "last_used_at", "revoked_at", "api_key"]),
  ApiKeyList: keys<ApiKeyList>()(["items"]),
  TraceListResponse: keys<TraceListResponse>()(["traces", "next_cursor"]),
  TraceSummary: keys<TraceSummary>()([
    "trace_id", "start_time", "end_time", "duration_ms", "status", "span_count", "error_span_count", "root_span_name", "environment", "resource",
  ]),
  TraceDetailResponse: keys<TraceDetailResponse>()([
    "trace_id", "status", "start_time", "end_time", "duration_ms", "span_count", "total_span_count", "truncated", "spans",
  ]),
  SpanOut: keys<SpanOut>()([
    "span_id", "parent_span_id", "name", "span_type", "resource", "start_time", "end_time", "duration_ms", "status", "status_message",
    "input", "input_size_bytes", "input_truncated", "output", "output_size_bytes", "output_truncated", "attributes", "attributes_truncated",
    "events", "events_truncated", "llm_provider", "llm_model", "llm_input_tokens", "llm_output_tokens", "llm_total_tokens", "llm_cost_usd",
    "environment", "release",
  ]),
  EvaluatorConfigOut: keys<EvaluatorConfigOut>()(["evaluator_name", "enabled", "sampling_rate", "threshold", "max_retries", "created_at", "updated_at"]),
  EvaluatorConfigListResponse: keys<EvaluatorConfigListResponse>()(["configs"]),
  EvaluationJobListResponse: keys<EvaluationJobListResponse>()(["jobs", "next_cursor"]),
  EvaluationJobOut: keys<EvaluationJobOut>()([
    "id", "trace_id", "span_id", "evaluator_name", "evaluator_version", "status", "attempt_count", "max_retries", "next_attempt_at",
    "claimed_at", "claimed_by", "last_error", "created_at", "updated_at",
  ]),
  SpanEvaluationsResponse: keys<SpanEvaluationsResponse>()(["results"]),
  EvaluationResultOut: keys<EvaluationResultOut>()([
    "evaluation_id", "trace_id", "span_id", "evaluator_name", "evaluator_version", "score", "label", "explanation", "evaluator_model",
    "evaluator_provider", "evaluation_latency_ms", "evaluation_cost_usd", "job_created_at", "written_at",
  ]),
  SpanAnalyticsResponse: keys<SpanAnalyticsResponse>()([
    "start_time_from", "start_time_to", "group_by", "bucket", "span_count", "error_span_count", "error_rate", "latency_ms", "groups", "buckets",
  ]),
  SpanAnalyticsBucket: keys<SpanAnalyticsBucket>()(["bucket_start", "span_count", "error_span_count", "error_rate", "latency_ms"]),
  LlmUsageResponse: keys<LlmUsageResponse>()([
    "start_time_from", "start_time_to", "group_by", "llm_span_count", "total_input_tokens", "total_output_tokens", "total_tokens",
    "total_cost_usd", "groups",
  ]),
};

function expectShape(value: unknown, name: keyof typeof CONTRACT) {
  expect(Object.keys(value as object).sort(), name).toEqual([...CONTRACT[name]].sort());
}

function randomHex(bytes: number): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (b) => b.toString(16).padStart(2, "0")).join("");
}

describe("Live Demo journey through the dashboard's /v1 contract", () => {
  it("covers all eight demo capabilities with exact response shapes", async () => {
    const email = `${unique()}@example.com`;
    const clientIp = { "x-vigil-dashboard-token": "test-dashboard-token", "x-vigil-client-ip": "192.0.2.50" };

    // 1. Sign up (dashboardAuth.signup) and the proxy's session check.
    const signup = await callJson("POST", "/v1/auth/signup", { headers: clientIp, body: { email, password: PASSWORD, full_name: "Demo User" } });
    expect(signup.status).toBe(201);
    // 2. Log in.
    const login = await callJson("POST", "/v1/auth/login", { headers: clientIp, body: { email, password: PASSWORD } });
    expect(login.status).toBe(200);
    const session = login.body.session_token as string;
    expect((await callJson("GET", "/v1/auth/session", { session })).status).toBe(200);
    const me0 = await callJson("GET", "/v1/me", { session });
    expectShape(me0.body, "Me");

    // 3. Create project (onboarding: organization, project, enable relevance).
    const org = await callJson("POST", "/v1/organizations", { session, body: { name: "Demo Org" } });
    expectShape(org.body, "Organization");
    const project = await callJson("POST", `/v1/organizations/${org.body.id}/projects`, { session, body: { name: "Demo Project" } });
    expectShape(project.body, "Project");
    const auth = { session, project: project.body.id };
    const config = await callJson("PUT", "/v1/evaluations/configs/relevance", { ...auth, body: { enabled: true, sampling_rate: 1 } });
    expectShape(config.body, "EvaluatorConfigOut");
    const me = await callJson("GET", "/v1/me", { session });
    expectShape(me.body.organizations[0], "Organization");
    expectShape(me.body.organizations[0].projects[0], "Project");

    // 4. Generate an API key.
    const key = await callJson("POST", `/v1/projects/${project.body.id}/api-keys`, { session, body: { name: "Live Demo" } });
    expectShape(key.body, "ApiKeyCreated");
    const keyList = await callJson("GET", `/v1/projects/${project.body.id}/api-keys`, { session });
    expectShape(keyList.body, "ApiKeyList");
    expectShape(keyList.body.items[0], "ApiKeyOut");
    expect(key.body.api_key).toMatch(/^vgl_[0-9a-f]+\.[A-Za-z0-9_-]+$/); // the dashboard's test-trace validation

    // 5. Send a test trace -- the exact payload of app/api/workspace/test-trace/route.ts.
    const end = new Date();
    const start = new Date(end.getTime() - 850);
    const traceId = randomHex(16);
    const spanId = randomHex(8);
    const ingest = await callJson("POST", "/v1/traces", {
      apiKey: key.body.api_key,
      body: {
        resource: { "service.name": "vigil-onboarding", "sdk.name": "vigil-dashboard-test" },
        spans: [{
          trace_id: traceId, span_id: spanId, parent_span_id: null, name: "onboarding test trace", span_type: "llm",
          start_time: start.toISOString(), end_time: end.toISOString(), status: "ok",
          input: "What is the capital of France?",
          output: "The capital of France is Paris.",
          environment: "onboarding-test",
        }],
      },
    });
    expect(ingest.status).toBe(200);
    expect(ingest.body.accepted).toBe(1);

    // 6. View traces, the trace, and the span.
    const list = await callJson("GET", "/v1/traces", auth);
    expectShape(list.body, "TraceListResponse");
    expectShape(list.body.traces[0], "TraceSummary");
    expect(list.body.traces[0]).toMatchObject({ trace_id: traceId, status: "ok", root_span_name: "onboarding test trace", resource: "vigil-onboarding" });
    const startDate = list.body.traces[0].start_time.slice(0, 10);
    const trace = await callJson("GET", `/v1/traces/${traceId}?start_date=${startDate}`, auth);
    expectShape(trace.body, "TraceDetailResponse");
    expectShape(trace.body.spans[0], "SpanOut");
    const span = await callJson("GET", `/v1/traces/${traceId}/spans/${spanId}?start_date=${startDate}`, auth);
    expect(span.body).toEqual(trace.body.spans[0]);
    expect(span.body.attributes).toEqual({ "resource.sdk.name": "vigil-dashboard-test" });

    // 7. TF-IDF relevance evaluation (ran synchronously during ingest).
    const results = await callJson("GET", `/v1/traces/${traceId}/spans/${spanId}/evaluations`, auth);
    expectShape(results.body, "SpanEvaluationsResponse");
    expectShape(results.body.results[0], "EvaluationResultOut");
    expect(results.body.results[0].evaluator_name).toBe("relevance");
    expect(results.body.results[0].score).toBeCloseTo(0.7093, 4);
    expect(results.body.results[0].label).toBe("relevant");
    const jobs = await callJson("GET", "/v1/evaluations/jobs", auth);
    expectShape(jobs.body, "EvaluationJobListResponse");
    expectShape(jobs.body.jobs[0], "EvaluationJobOut");
    expect(jobs.body.jobs[0].status).toBe("succeeded");
    const configs = await callJson("GET", "/v1/evaluations/configs", auth);
    expectShape(configs.body, "EvaluatorConfigListResponse");

    // 8. Dashboard analytics (overview + analytics pages).
    const spans = await callJson("GET", "/v1/analytics/spans", auth);
    expectShape(spans.body, "SpanAnalyticsResponse");
    expect(spans.body.span_count).toBe(1);
    const buckets = await callJson("GET", "/v1/analytics/spans?bucket=hour", auth);
    expectShape(buckets.body.buckets[0], "SpanAnalyticsBucket");
    const llm = await callJson("GET", "/v1/analytics/llm-usage?group_by=llm_model", auth);
    expectShape(llm.body, "LlmUsageResponse");

    // Log out: the session stops working immediately.
    expect((await callJson("POST", "/v1/auth/logout", { session })).status).toBe(204);
    expect((await callJson("GET", "/v1/traces", auth)).status).toBe(401);
  });
});
