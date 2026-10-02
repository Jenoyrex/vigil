// Vigil public demo API: a Cloudflare Worker serving the /v1 contract the
// dashboard uses, backed by D1. See docs/decisions/009-public-demo-architecture.md.

import { cleanup } from "./cleanup";
import type { Env } from "./config";
import { ApiError, errorResponse, json } from "./http";
import * as analytics from "./routes/analytics";
import * as auth from "./routes/auth";
import * as evaluations from "./routes/evaluations";
import * as traces from "./routes/traces";
import * as workspace from "./routes/workspace";

type Handler = (request: Request, env: Env, ...params: string[]) => Promise<Response>;

const path = (template: string) => new RegExp(`^${template.replace(/\{\w+\}/g, "([^/]+)")}$`);

const ROUTES: [string, RegExp, Handler][] = [
  ["POST", path("/v1/auth/signup"), auth.signup],
  ["POST", path("/v1/auth/login"), auth.login],
  ["GET", path("/v1/auth/session"), auth.session],
  ["POST", path("/v1/auth/logout"), auth.logout],
  ["GET", path("/v1/me"), workspace.me],
  ["POST", path("/v1/organizations"), workspace.createOrganization],
  ["POST", path("/v1/organizations/{organization_id}/projects"), workspace.createProject],
  ["GET", path("/v1/projects/{project_id}/api-keys"), workspace.listApiKeys],
  ["POST", path("/v1/projects/{project_id}/api-keys"), workspace.createApiKey],
  ["POST", path("/v1/projects/{project_id}/api-keys/{key_id}/revoke"), workspace.revokeApiKey],
  ["POST", path("/v1/traces"), traces.ingestTraces],
  ["GET", path("/v1/traces"), traces.listTraces],
  ["GET", path("/v1/traces/{trace_id}"), traces.getTrace],
  ["GET", path("/v1/traces/{trace_id}/spans/{span_id}"), traces.getSpan],
  ["GET", path("/v1/traces/{trace_id}/spans/{span_id}/evaluations"), evaluations.listSpanEvaluations],
  ["GET", path("/v1/evaluations/configs"), evaluations.listConfigs],
  ["GET", path("/v1/evaluations/configs/{evaluator_name}"), evaluations.getConfig],
  ["PUT", path("/v1/evaluations/configs/{evaluator_name}"), evaluations.upsertConfig],
  ["GET", path("/v1/evaluations/jobs"), evaluations.listJobs],
  ["GET", path("/v1/analytics/spans"), analytics.spanAnalytics],
  ["GET", path("/v1/analytics/llm-usage"), analytics.llmUsage],
];

async function route(request: Request, env: Env): Promise<Response> {
  const { pathname } = new URL(request.url);
  const allowed: string[] = [];
  for (const [method, pattern, handler] of ROUTES) {
    const match = pattern.exec(pathname);
    if (!match) continue;
    if (method !== request.method) {
      allowed.push(method);
      continue;
    }
    return handler(request, env, ...match.slice(1).map(decodeURIComponent));
  }
  if (allowed.length > 0) return json({ detail: "Method Not Allowed" }, 405, { Allow: allowed.join(", ") });
  if (pathname === "/" || pathname === "/health") return json({ status: "ok", service: "vigil-demo-api" });
  return json({ detail: "Not Found" }, 404);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      return await route(request, env);
    } catch (error) {
      if (error instanceof ApiError) return errorResponse(error);
      console.error("unhandled error", error instanceof Error ? error.stack : String(error));
      return json({ detail: "Internal Server Error" }, 500);
    }
  },

  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    console.log("demo cleanup", JSON.stringify(await cleanup(env)));
  },
} satisfies ExportedHandler<Env>;
