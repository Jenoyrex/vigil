import "server-only";

import { apiFetch, PROJECT_ID_HEADER, SESSION_TOKEN_HEADER, type QueryParams } from "./http";
import { requireCurrentProject } from "./workspace";

export type { QueryParams };

/**
 * Project-scoped calls to apps/api (traces, analytics, evaluations), made
 * as the signed-in user for the project they're currently viewing: the
 * dashboard session token plus `X-Vigil-Project-Id`. apps/api checks that
 * the user belongs to that project's organization on every call
 * (app.api.deps.get_project_access) -- this app never holds a shared API
 * key that could read every tenant's data.
 *
 * Callers are Server Components (initial render) and the app/api/vigil/**
 * route handlers (the same-origin BFF Client Components fetch from).
 */

export interface VigilAuth {
  sessionToken: string;
  projectId: string;
}

export interface VigilFetchInit {
  method?: "PUT";
  body?: unknown;
  /** Explicit credentials -- e.g. configuring a project created in this same request. */
  auth?: VigilAuth;
}

async function currentAuth(): Promise<VigilAuth> {
  const { sessionToken, currentProject } = await requireCurrentProject();
  return { sessionToken, projectId: currentProject.id };
}

export async function vigilFetch<T>(
  path: string,
  params?: QueryParams,
  init?: VigilFetchInit,
): Promise<T> {
  const auth = init?.auth ?? (await currentAuth());
  return apiFetch<T>(path, params, {
    method: init?.method,
    body: init?.body,
    headers: { [SESSION_TOKEN_HEADER]: auth.sessionToken, [PROJECT_ID_HEADER]: auth.projectId },
  });
}
