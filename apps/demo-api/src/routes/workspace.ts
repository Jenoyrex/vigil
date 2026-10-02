// /v1/me, organizations, projects, project API keys -- mirrors
// apps/api/app/api/v1/workspace.py and services/workspace.py.

import type {
  ApiKeyCreated,
  ApiKeyList,
  ApiKeyOut,
  Me,
  Organization,
  Project,
} from "../../../dashboard/lib/api/workspaceTypes";
import { projectRole, requireUser } from "../auth";
import { readJsonObject } from "../body";
import {
  type Env,
  MAX_API_KEYS_PER_PROJECT,
  MAX_ORGANIZATIONS_PER_USER,
  MAX_PROJECTS_PER_ORGANIZATION,
} from "../config";
import { ApiError, isoTime, isoTimeOrNull, json } from "../http";
import { generateApiKey, randomHex } from "../security";
import { Validator, pathUuid, requiredStr } from "../validation";

const MANAGER_ROLES = new Set(["owner", "admin"]);
const notFound = () => new ApiError(404, "Not found.");
const forbidden = () => new ApiError(403, "Only organization owners and admins can do this.");
const demoLimit = (what: string, max: number) =>
  new ApiError(403, `Public demo limit reached: at most ${max} ${what}. Demo data is removed automatically after a period of inactivity.`);

/** apps/api's _slug: lowercase, non-alphanumerics to '-', 40 chars, random suffix. */
function slug(name: string): string {
  const base =
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40)
      .replace(/^-+|-+$/g, "") || "workspace";
  return `${base}-${randomHex(4)}`;
}

async function readName(request: Request): Promise<string> {
  const body = await readJsonObject(request, 64 * 1024);
  const v = new Validator();
  const name = requiredStr(body, "name", ["body"], v, { stripWhitespace: true, minLength: 1, maxLength: 200 });
  v.throwIfInvalid();
  return name as string;
}

function pathId(raw: string, name: string): string {
  const v = new Validator();
  const id = pathUuid(raw, name, v);
  v.throwIfInvalid();
  return id as string;
}

interface ProjectRow {
  id: string;
  organization_id: string;
  name: string;
  slug: string;
  created_at: number;
}

const projectOut = (p: ProjectRow): Project => ({ id: p.id, name: p.name, slug: p.slug, created_at: isoTime(p.created_at) });

export async function me(request: Request, env: Env): Promise<Response> {
  const session = await requireUser(request, env);
  const [user, orgs, projects] = await env.DB.batch([
    env.DB.prepare("SELECT id, email, full_name FROM users WHERE id = ?").bind(session.userId),
    env.DB.prepare(
      `SELECT o.id, o.name, o.slug, m.role FROM organizations o
         JOIN organization_memberships m ON m.organization_id = o.id
        WHERE m.user_id = ? ORDER BY o.created_at, o.id`,
    ).bind(session.userId),
    env.DB.prepare(
      `SELECT p.* FROM projects p
         JOIN organization_memberships m ON m.organization_id = p.organization_id
        WHERE m.user_id = ? ORDER BY p.created_at, p.id`,
    ).bind(session.userId),
  ]);
  const u = user.results[0] as { id: string; email: string; full_name: string | null };
  const projectRows = projects.results as unknown as ProjectRow[];
  const body: Me = {
    user: { id: u.id, email: u.email, full_name: u.full_name },
    organizations: (orgs.results as unknown as { id: string; name: string; slug: string; role: Organization["role"] }[]).map(
      (o) => ({
        id: o.id,
        name: o.name,
        slug: o.slug,
        role: o.role,
        projects: projectRows.filter((p) => p.organization_id === o.id).map(projectOut),
      }),
    ),
  };
  return json(body);
}

export async function createOrganization(request: Request, env: Env): Promise<Response> {
  const session = await requireUser(request, env);
  const name = await readName(request);
  const owned = await env.DB.prepare("SELECT COUNT(*) AS n FROM organization_memberships WHERE user_id = ?")
    .bind(session.userId)
    .first<{ n: number }>();
  if ((owned?.n ?? 0) >= MAX_ORGANIZATIONS_PER_USER) throw demoLimit("organizations per account", MAX_ORGANIZATIONS_PER_USER);

  const id = crypto.randomUUID();
  const now = Date.now();
  const orgSlug = slug(name);
  await env.DB.batch([
    env.DB.prepare("INSERT INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)").bind(id, name, orgSlug, now),
    env.DB.prepare(
      "INSERT INTO organization_memberships (id, user_id, organization_id, role, created_at) VALUES (?, ?, ?, 'owner', ?)",
    ).bind(crypto.randomUUID(), session.userId, id, now),
  ]);
  const body: Organization = { id, name, slug: orgSlug, role: "owner", projects: [] };
  return json(body, 201);
}

export async function createProject(request: Request, env: Env, organizationIdRaw: string): Promise<Response> {
  const session = await requireUser(request, env);
  const organizationId = pathId(organizationIdRaw, "organization_id");
  const name = await readName(request);
  const membership = await env.DB.prepare(
    "SELECT role FROM organization_memberships WHERE organization_id = ? AND user_id = ?",
  )
    .bind(organizationId, session.userId)
    .first<{ role: string }>();
  if (!membership) throw notFound();
  if (!MANAGER_ROLES.has(membership.role)) throw forbidden();
  const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM projects WHERE organization_id = ?")
    .bind(organizationId)
    .first<{ n: number }>();
  if ((count?.n ?? 0) >= MAX_PROJECTS_PER_ORGANIZATION) throw demoLimit("projects per organization", MAX_PROJECTS_PER_ORGANIZATION);

  const project: ProjectRow = {
    id: crypto.randomUUID(),
    organization_id: organizationId,
    name,
    slug: slug(name),
    created_at: Date.now(),
  };
  await env.DB.prepare("INSERT INTO projects (id, organization_id, name, slug, created_at) VALUES (?, ?, ?, ?, ?)")
    .bind(project.id, project.organization_id, project.name, project.slug, project.created_at)
    .run();
  return json(projectOut(project), 201);
}

async function requireProjectRole(env: Env, userId: string, projectId: string, manage: boolean): Promise<void> {
  const role = await projectRole(env, userId, projectId);
  if (role === null) throw notFound();
  if (manage && !MANAGER_ROLES.has(role)) throw forbidden();
}

interface KeyRow {
  id: string;
  name: string;
  key_prefix: string;
  status: ApiKeyOut["status"];
  created_at: number;
  last_used_at: number | null;
  revoked_at: number | null;
}

const keyOut = (k: KeyRow): ApiKeyOut => ({
  id: k.id,
  name: k.name,
  key_prefix: k.key_prefix,
  status: k.status,
  created_at: isoTime(k.created_at),
  last_used_at: isoTimeOrNull(k.last_used_at),
  revoked_at: isoTimeOrNull(k.revoked_at),
});

const KEY_COLUMNS = "id, name, key_prefix, status, created_at, last_used_at, revoked_at";

export async function listApiKeys(request: Request, env: Env, projectIdRaw: string): Promise<Response> {
  const session = await requireUser(request, env);
  const projectId = pathId(projectIdRaw, "project_id");
  await requireProjectRole(env, session.userId, projectId, false);
  const { results } = await env.DB.prepare(
    `SELECT ${KEY_COLUMNS} FROM api_keys WHERE project_id = ? ORDER BY created_at DESC, id DESC`,
  )
    .bind(projectId)
    .all<KeyRow>();
  const body: ApiKeyList = { items: results.map(keyOut) };
  return json(body);
}

export async function createApiKey(request: Request, env: Env, projectIdRaw: string): Promise<Response> {
  const session = await requireUser(request, env);
  const projectId = pathId(projectIdRaw, "project_id");
  const name = await readName(request);
  await requireProjectRole(env, session.userId, projectId, true);
  const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM api_keys WHERE project_id = ?").bind(projectId).first<{ n: number }>();
  if ((count?.n ?? 0) >= MAX_API_KEYS_PER_PROJECT) throw demoLimit("API keys per project", MAX_API_KEYS_PER_PROJECT);

  const { rawKey, keyPrefix, keyHash } = await generateApiKey();
  const row: KeyRow = {
    id: crypto.randomUUID(),
    name,
    key_prefix: keyPrefix,
    status: "active",
    created_at: Date.now(),
    last_used_at: null,
    revoked_at: null,
  };
  await env.DB.prepare(
    "INSERT INTO api_keys (id, project_id, name, key_prefix, key_hash, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
  )
    .bind(row.id, projectId, name, keyPrefix, keyHash, session.userId, row.created_at)
    .run();
  const body: ApiKeyCreated = { ...keyOut(row), api_key: rawKey };
  return json(body, 201);
}

export async function revokeApiKey(request: Request, env: Env, projectIdRaw: string, keyIdRaw: string): Promise<Response> {
  const session = await requireUser(request, env);
  const projectId = pathId(projectIdRaw, "project_id");
  const keyId = pathId(keyIdRaw, "key_id");
  await requireProjectRole(env, session.userId, projectId, true);
  await env.DB.prepare(
    "UPDATE api_keys SET status = 'revoked', revoked_at = ? WHERE id = ? AND project_id = ? AND status != 'revoked'",
  )
    .bind(Date.now(), keyId, projectId)
    .run();
  const row = await env.DB.prepare(`SELECT ${KEY_COLUMNS} FROM api_keys WHERE id = ? AND project_id = ?`)
    .bind(keyId, projectId)
    .first<KeyRow>();
  if (!row) throw notFound();
  return json(keyOut(row));
}
