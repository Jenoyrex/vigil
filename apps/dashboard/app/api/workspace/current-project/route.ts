import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { PROJECT_COOKIE_NAME } from "@/lib/api/dashboardAuth";
import { getWorkspace, projectCookieOptions } from "@/lib/api/workspace";

import { badRequest, readJson, requiredString } from "../_lib";

/** POST /api/workspace/current-project -- switch which project the dashboard shows. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const parsed = await readJson(request);
  if ("error" in parsed) return parsed.error;
  const projectId = requiredString(parsed.body, "projectId");
  if (!projectId) return badRequest("projectId is required.");

  const workspace = await getWorkspace();
  const visible = workspace?.me.organizations.some((org) => org.projects.some((p) => p.id === projectId));
  if (!visible) return NextResponse.json({ detail: "Project not found." }, { status: 404 });

  const response = NextResponse.json({ ok: true });
  response.cookies.set(PROJECT_COOKIE_NAME, projectId, projectCookieOptions());
  return response;
}
