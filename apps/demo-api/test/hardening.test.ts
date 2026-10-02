import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { cleanup } from "../src/cleanup";
import { RATE_LIMITS } from "../src/config";
import { enforceRateLimit } from "../src/rateLimit";
import { PASSWORD, callJson, signup, unique, workspace } from "./helpers";

const T = (n: number) => n.toString(16).padStart(32, "0");
const S = (n: number) => n.toString(16).padStart(16, "0");
const DAY = 86400e3;

const signupFrom = (headers: Record<string, string>) =>
  callJson("POST", "/v1/auth/signup", { headers, body: { email: `${unique()}@example.com`, password: PASSWORD } });

describe("per-IP rate limiting", () => {
  it("limits signups per client IP with a Retry-After header", async () => {
    const ip = { "cf-connecting-ip": "203.0.113.10" };
    for (let i = 0; i < 5; i++) expect((await signupFrom(ip)).status).toBe(201);
    const limited = await signupFrom(ip);
    expect(limited.status).toBe(429);
    expect(limited.body.detail).toMatch(/^Rate limit exceeded\. Retry after \d+ seconds\.$/);
    expect(Number(limited.response.headers.get("retry-after"))).toBeGreaterThan(0);
    expect((await signupFrom({ "cf-connecting-ip": "203.0.113.11" })).status).toBe(201);
  });

  it("attributes dashboard traffic to the forwarded client IP only with the shared token", async () => {
    const dashboard = "198.51.100.1"; // the dashboard server's own IP, shared by all users
    const forwarded = (client: string, token = "test-dashboard-token") => ({
      "cf-connecting-ip": dashboard, "x-vigil-dashboard-token": token, "x-vigil-client-ip": client,
    });
    for (let i = 0; i < 5; i++) expect((await signupFrom(forwarded("192.0.2.1"))).status).toBe(201);
    expect((await signupFrom(forwarded("192.0.2.1"))).status).toBe(429);
    // A different end user behind the same dashboard server is unaffected.
    expect((await signupFrom(forwarded("192.0.2.2"))).status).toBe(201);
    // Without the right token the header is ignored and the peer IP is used.
    for (let i = 0; i < 5; i++) expect((await signupFrom(forwarded(`192.0.2.${10 + i}`, "wrong"))).status).toBe(201);
    expect((await signupFrom(forwarded("192.0.2.99", "wrong"))).status).toBe(429);
  });

  it("limits login attempts per account", async () => {
    const { email } = await signup();
    for (let i = 0; i < 10; i++) {
      const res = await callJson("POST", "/v1/auth/login", { body: { email, password: "wrong password!!" } });
      expect(res.status).toBe(401);
    }
    expect((await callJson("POST", "/v1/auth/login", { body: { email, password: PASSWORD } })).status).toBe(429);
  });

  it("limits ingestion per API key", async () => {
    const ws = await workspace({ relevance: false });
    const body = { spans: [{ trace_id: T(1), span_id: S(1), name: "s", start_time: new Date().toISOString(), end_time: new Date().toISOString() }] };
    expect((await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body })).status).toBe(200);
    // Fill the rest of this key's current window instead of sending 60 requests.
    await env.DB.prepare("UPDATE rate_limits SET count = ? WHERE key LIKE 'ingest-key:%'").bind(RATE_LIMITS.ingestPerKey.limit).run();
    const limited = await callJson("POST", "/v1/traces", { apiKey: ws.apiKey, body });
    expect(limited.status).toBe(429);
    expect(limited.response.headers.get("retry-after")).toMatch(/^\d+$/);
  });

  it("counts per fixed window and resets in the next one", async () => {
    const rule = { name: "unit", limit: 2, windowSeconds: 60 };
    const t0 = 1_800_000_000_000; // aligned to a 60 s window
    await enforceRateLimit(env, rule, "a", t0);
    await enforceRateLimit(env, rule, "a", t0 + 1000);
    await expect(enforceRateLimit(env, rule, "a", t0 + 59_000)).rejects.toMatchObject({ status: 429, headers: { "Retry-After": "1" } });
    await expect(enforceRateLimit(env, rule, "b", t0 + 59_000)).resolves.toBeUndefined();
    await expect(enforceRateLimit(env, rule, "a", t0 + 60_000)).resolves.toBeUndefined();
  });
});

describe("scheduled cleanup", () => {
  it("deletes telemetry past retention and inactive accounts with everything they own", async () => {
    const fresh = await workspace();
    const stale = await workspace();
    const now = Date.now();
    const span = (n: number) => ({ trace_id: T(n), span_id: S(n), name: "chat", span_type: "llm", input: "capital of France", output: "Paris is the capital of France", start_time: new Date(now - 1000).toISOString(), end_time: new Date(now).toISOString() });
    await callJson("POST", "/v1/traces", { apiKey: fresh.apiKey, body: { spans: [span(1), span(2)] } });
    await callJson("POST", "/v1/traces", { apiKey: stale.apiKey, body: { spans: [span(3)] } });
    // Age one of fresh's spans (and its evaluation) past telemetry retention.
    await env.DB.prepare("UPDATE spans SET ingested_at = ? WHERE project_id = ? AND trace_id = ?").bind(now - 8 * DAY, fresh.projectId, T(1)).run();
    await env.DB.prepare("UPDATE evaluation_jobs SET created_at = ? WHERE project_id = ? AND trace_id = ?").bind(now - 8 * DAY, fresh.projectId, T(1)).run();
    // Make the stale account inactive for 31 days.
    const staleUser = await env.DB.prepare("SELECT id FROM users WHERE email = ?").bind(stale.email).first<{ id: string }>();
    await env.DB.prepare("UPDATE users SET created_at = ? WHERE id = ?").bind(now - 40 * DAY, staleUser!.id).run();
    await env.DB.prepare("UPDATE dashboard_sessions SET created_at = ? WHERE user_id = ?").bind(now - 31 * DAY, staleUser!.id).run();

    const counts = await cleanup(env, now);
    expect(counts.spans).toBeGreaterThanOrEqual(1);
    expect(counts.users).toBeGreaterThanOrEqual(1);

    const count = async (sql: string, ...args: unknown[]) => (await env.DB.prepare(sql).bind(...args).first<{ n: number }>())!.n;
    expect(await count("SELECT COUNT(*) AS n FROM spans WHERE project_id = ?", fresh.projectId)).toBe(1);
    expect(await count("SELECT COUNT(*) AS n FROM evaluation_jobs WHERE project_id = ?", fresh.projectId)).toBe(1);
    expect(await count("SELECT COUNT(*) AS n FROM evaluation_results WHERE project_id = ?", fresh.projectId)).toBe(1);
    for (const table of ["projects WHERE id", "spans WHERE project_id", "api_keys WHERE project_id", "evaluation_jobs WHERE project_id"]) {
      expect(await count(`SELECT COUNT(*) AS n FROM ${table} = ?`, stale.projectId)).toBe(0);
    }
    expect(await count("SELECT COUNT(*) AS n FROM organizations WHERE id = ?", stale.organizationId)).toBe(0);
    // The active account still works end to end.
    expect((await callJson("GET", "/v1/traces", { apiKey: fresh.apiKey })).body.traces).toHaveLength(1);
    expect((await callJson("GET", "/v1/me", { session: stale.session })).status).toBe(401);
  });
});
