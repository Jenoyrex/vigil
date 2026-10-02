// Request authentication, mirroring apps/api/app/api/deps.py (same order of
// checks, same status codes and messages).

import type { Env } from "./config";
import { ApiError } from "./http";
import { hasExpectedKeyShape, sha256Hex } from "./security";
import { parseUuid } from "./validation";

export const SESSION_TOKEN_HEADER = "X-Vigil-Session-Token";
const PROJECT_ID_HEADER = "X-Vigil-Project-Id";

const INVALID_KEY = "Invalid or missing API key.";
const REVOKED_KEY = "This API key has been revoked.";
const INVALID_SESSION = "Invalid or expired session.";
const PROJECT_NOT_FOUND = "Project not found.";

const unauthorizedBearer = (detail: string) => new ApiError(401, detail, { "WWW-Authenticate": "Bearer" });
const invalidSession = () => new ApiError(401, INVALID_SESSION);

interface AuthenticatedSession {
  sessionId: string;
  userId: string;
  email: string;
  expiresAt: number;
}

interface ProjectAccess {
  projectId: string;
}

/** FastAPI HTTPBearer(auto_error=False): credentials, or null. */
function bearerCredentials(request: Request): string | null {
  const header = request.headers.get("authorization");
  if (!header) return null;
  const [scheme, ...rest] = header.trim().split(" ");
  const credentials = rest.join(" ").trim();
  if (!scheme || !credentials || scheme.toLowerCase() !== "bearer") return null;
  return credentials;
}

async function validateSession(env: Env, rawToken: string): Promise<AuthenticatedSession | null> {
  const row = await env.DB.prepare(
    `SELECT s.id, s.user_id, s.expires_at, s.revoked_at, u.email, u.is_active
       FROM dashboard_sessions s JOIN users u ON u.id = s.user_id
      WHERE s.token_hash = ?`,
  )
    .bind(await sha256Hex(rawToken))
    .first<{ id: string; user_id: string; expires_at: number; revoked_at: number | null; email: string; is_active: number }>();
  if (!row || row.revoked_at !== null || row.expires_at <= Date.now() || !row.is_active) return null;
  return { sessionId: row.id, userId: row.user_id, email: row.email, expiresAt: row.expires_at };
}

export async function requireUser(request: Request, env: Env): Promise<AuthenticatedSession> {
  const token = request.headers.get(SESSION_TOKEN_HEADER);
  if (token === null) throw invalidSession();
  const session = await validateSession(env, token);
  if (!session) throw invalidSession();
  return session;
}

interface AuthenticatedKey {
  apiKeyId: string;
  projectId: string;
}

export async function requireApiKey(request: Request, env: Env): Promise<AuthenticatedKey> {
  const raw = bearerCredentials(request);
  if (raw === null || !hasExpectedKeyShape(raw)) throw unauthorizedBearer(INVALID_KEY);
  return authenticateRawKey(env, raw);
}

async function authenticateRawKey(env: Env, raw: string): Promise<AuthenticatedKey> {
  const row = await env.DB.prepare("SELECT id, project_id, status FROM api_keys WHERE key_hash = ?")
    .bind(await sha256Hex(raw))
    .first<{ id: string; project_id: string; status: string }>();
  if (!row) throw unauthorizedBearer(INVALID_KEY);
  if (row.status !== "active") throw unauthorizedBearer(REVOKED_KEY);
  await env.DB.prepare("UPDATE api_keys SET last_used_at = ? WHERE id = ?").bind(Date.now(), row.id).run();
  return { apiKeyId: row.id, projectId: row.project_id };
}

export async function projectRole(env: Env, userId: string, projectId: string): Promise<string | null> {
  const row = await env.DB.prepare(
    `SELECT m.role FROM organization_memberships m
       JOIN projects p ON p.organization_id = m.organization_id
      WHERE p.id = ? AND m.user_id = ?`,
  )
    .bind(projectId, userId)
    .first<{ role: string }>();
  return row?.role ?? null;
}

/**
 * apps/api's get_project_access: an API key (Bearer) wins; otherwise a
 * dashboard session plus X-Vigil-Project-Id naming a project the user is a
 * member of. A foreign project is indistinguishable from a missing one (404).
 */
export async function requireProjectAccess(request: Request, env: Env): Promise<ProjectAccess> {
  const raw = bearerCredentials(request);
  if (raw !== null) {
    if (!hasExpectedKeyShape(raw)) throw unauthorizedBearer(INVALID_KEY);
    return authenticateRawKey(env, raw);
  }
  const token = request.headers.get(SESSION_TOKEN_HEADER);
  if (token === null) throw unauthorizedBearer(INVALID_KEY);
  const session = await validateSession(env, token);
  if (!session) throw invalidSession();
  const projectId = parseUuid(request.headers.get(PROJECT_ID_HEADER));
  if (projectId === null || (await projectRole(env, session.userId, projectId)) === null) {
    throw new ApiError(404, PROJECT_NOT_FOUND);
  }
  return { projectId };
}
