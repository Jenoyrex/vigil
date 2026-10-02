import { SELF } from "cloudflare:test";

export interface CallOptions {
  body?: unknown;
  rawBody?: string;
  headers?: Record<string, string>;
  session?: string;
  project?: string;
  apiKey?: string;
  ip?: string;
}

export async function call(method: string, path: string, opts: CallOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { "cf-connecting-ip": opts.ip ?? uniqueIp(), ...opts.headers };
  if (opts.session) headers["X-Vigil-Session-Token"] = opts.session;
  if (opts.project) headers["X-Vigil-Project-Id"] = opts.project;
  if (opts.apiKey) headers.Authorization = `Bearer ${opts.apiKey}`;
  let body: string | undefined = opts.rawBody;
  if (opts.body !== undefined) {
    body = JSON.stringify(opts.body);
    headers["content-type"] = "application/json";
  }
  return SELF.fetch(`https://demo.test${path}`, { method, headers, body });
}

export async function callJson<T = any>(method: string, path: string, opts: CallOptions = {}): Promise<{ status: number; body: T; response: Response }> {
  const response = await call(method, path, opts);
  const text = await response.text();
  return { status: response.status, body: (text ? JSON.parse(text) : null) as T, response };
}

let counter = 0;
export function unique(prefix = "u"): string {
  counter += 1;
  return `${prefix}${Date.now().toString(36)}${counter}${Math.random().toString(36).slice(2, 6)}`;
}

/** A distinct client IP per call keeps per-IP rate limits out of unrelated tests. */
export function uniqueIp(): string {
  const n = Math.floor(Math.random() * 2 ** 24);
  return `10.${(n >> 16) & 255}.${(n >> 8) & 255}.${n & 255}`;
}

export const PASSWORD = "correct horse battery staple";

export interface Workspace {
  email: string;
  session: string;
  organizationId: string;
  projectId: string;
  apiKey: string;
}

export async function signup(email = `${unique()}@example.com`): Promise<{ email: string; session: string }> {
  const res = await callJson("POST", "/v1/auth/signup", { body: { email, password: PASSWORD } });
  if (res.status !== 201) throw new Error(`signup failed: ${res.status} ${JSON.stringify(res.body)}`);
  return { email, session: res.body.session_token };
}

/** A signed-up user with an organization, a project (relevance enabled) and an API key. */
export async function workspace(opts: { relevance?: boolean } = {}): Promise<Workspace> {
  const { email, session } = await signup();
  const org = await callJson("POST", "/v1/organizations", { session, body: { name: "Acme" } });
  const project = await callJson("POST", `/v1/organizations/${org.body.id}/projects`, { session, body: { name: "Demo" } });
  if (opts.relevance !== false) {
    await callJson("PUT", "/v1/evaluations/configs/relevance", {
      session,
      project: project.body.id,
      body: { enabled: true, sampling_rate: 1 },
    });
  }
  const key = await callJson("POST", `/v1/projects/${project.body.id}/api-keys`, { session, body: { name: "k" } });
  return { email, session, organizationId: org.body.id, projectId: project.body.id, apiKey: key.body.api_key };
}

export const ISO_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?Z$/;
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
