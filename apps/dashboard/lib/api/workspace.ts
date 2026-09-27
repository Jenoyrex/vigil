import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";

import { PROJECT_COOKIE_NAME, SESSION_COOKIE_NAME } from "./dashboardAuth";
import { apiFetch, SESSION_TOKEN_HEADER } from "./http";
import { VigilApiError } from "./types";
import type { ApiKeyCreated, ApiKeyList, ApiKeyOut, Me, Organization, Project } from "./workspaceTypes";

/**
 * The signed-in user's workspace: who they are, their organizations and
 * projects (GET /v1/me), and which project they're currently viewing.
 *
 * The current project is remembered in `PROJECT_COOKIE_NAME`, but that
 * cookie is only a preference -- it is honored only when it names a
 * project this user can actually see according to /v1/me, else the first
 * project wins. apps/api independently re-checks membership on every
 * project-scoped call anyway (app.api.deps.get_project_access), so a
 * tampered cookie can at worst select nothing.
 */

export { PROJECT_COOKIE_NAME };

export interface Workspace {
  me: Me;
  sessionToken: string;
  currentOrganization: Organization | null;
  currentProject: Project | null;
}

export function projectCookieOptions() {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict" as const,
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
  };
}

function sessionHeaders(sessionToken: string): Record<string, string> {
  return { [SESSION_TOKEN_HEADER]: sessionToken };
}

/** Cached per request (React `cache`), so layout + page + data calls share one /v1/me. */
export const getWorkspace = cache(async (): Promise<Workspace | null> => {
  const jar = await cookies();
  const sessionToken = jar.get(SESSION_COOKIE_NAME)?.value;
  if (!sessionToken) return null;

  let me: Me;
  try {
    me = await apiFetch<Me>("/v1/me", undefined, { headers: sessionHeaders(sessionToken) });
  } catch (error) {
    if (error instanceof VigilApiError && error.status === 401) return null;
    throw error;
  }

  const preferred = jar.get(PROJECT_COOKIE_NAME)?.value;
  const pairs = me.organizations.flatMap((org) => org.projects.map((project) => ({ org, project })));
  const current = pairs.find((pair) => pair.project.id === preferred) ?? pairs[0];

  return {
    me,
    sessionToken,
    currentOrganization: current?.org ?? me.organizations[0] ?? null,
    currentProject: current?.project ?? null,
  };
});

/**
 * For pages that show project data: a signed-in user without a project yet
 * belongs in onboarding, not in an empty dashboard.
 */
export async function requireProject(): Promise<Workspace & { currentProject: Project }> {
  const workspace = await getWorkspace();
  if (!workspace) redirect("/login");
  if (!workspace.currentProject) redirect("/onboarding");
  return workspace as Workspace & { currentProject: Project };
}

export async function requireSessionToken(): Promise<string> {
  const token = (await cookies()).get(SESSION_COOKIE_NAME)?.value;
  if (!token) throw new VigilApiError(401, "Invalid or expired session.");
  return token;
}

export async function createOrganization(name: string): Promise<Organization> {
  return apiFetch<Organization>("/v1/organizations", undefined, {
    method: "POST",
    body: { name },
    headers: sessionHeaders(await requireSessionToken()),
  });
}

export async function createProject(organizationId: string, name: string): Promise<Project> {
  return apiFetch<Project>(
    `/v1/organizations/${encodeURIComponent(organizationId)}/projects`,
    undefined,
    { method: "POST", body: { name }, headers: sessionHeaders(await requireSessionToken()) },
  );
}

export async function listApiKeys(projectId: string): Promise<ApiKeyList> {
  return apiFetch<ApiKeyList>(`/v1/projects/${encodeURIComponent(projectId)}/api-keys`, undefined, {
    headers: sessionHeaders(await requireSessionToken()),
  });
}

export async function createApiKey(projectId: string, name: string): Promise<ApiKeyCreated> {
  return apiFetch<ApiKeyCreated>(
    `/v1/projects/${encodeURIComponent(projectId)}/api-keys`,
    undefined,
    { method: "POST", body: { name }, headers: sessionHeaders(await requireSessionToken()) },
  );
}

export async function revokeApiKey(projectId: string, keyId: string): Promise<ApiKeyOut> {
  return apiFetch<ApiKeyOut>(
    `/v1/projects/${encodeURIComponent(projectId)}/api-keys/${encodeURIComponent(keyId)}/revoke`,
    undefined,
    { method: "POST", headers: sessionHeaders(await requireSessionToken()) },
  );
}

/**
 * The public ingestion URL shown in quickstart snippets. Defaults to the
 * URL this server itself uses for apps/api; set VIGIL_PUBLIC_API_BASE_URL
 * when that one is internal-only (e.g. `http://api:8000` in Docker).
 */
export function publicApiBaseUrl(): string {
  return (process.env.VIGIL_PUBLIC_API_BASE_URL || process.env.VIGIL_API_BASE_URL || "").replace(
    /\/+$/,
    "",
  );
}

/** Route-handler variant of `requireProject`: throws a VigilApiError instead of redirecting. */
export async function requireCurrentProject(): Promise<Workspace & { currentProject: Project }> {
  const workspace = await getWorkspace();
  if (!workspace) throw new VigilApiError(401, "Invalid or expired session.");
  if (!workspace.currentProject) {
    throw new VigilApiError(409, "Create a project to start sending traces.");
  }
  return workspace as Workspace & { currentProject: Project };
}
