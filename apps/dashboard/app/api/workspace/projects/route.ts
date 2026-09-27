import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { PROJECT_COOKIE_NAME } from "@/lib/api/dashboardAuth";
import { vigilFetch } from "@/lib/api/vigilClient";
import { createProject, projectCookieOptions, requireSessionToken } from "@/lib/api/workspace";

import { handleVigilError } from "../../vigil/_lib/handleError";
import { badRequest, readJson, requiredString } from "../_lib";

/**
 * POST /api/workspace/projects -- creates a project and makes it the one
 * being viewed. With `enableRelevance`, also enables the built-in `relevance`
 * evaluator for it through the ordinary evaluator-config API
 * (PUT /v1/evaluations/configs/relevance), sampling every LLM span -- the
 * same setting a user could make on the Evaluations page.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const parsed = await readJson(request);
  if ("error" in parsed) return parsed.error;
  const organizationId = requiredString(parsed.body, "organizationId");
  const name = requiredString(parsed.body, "name");
  if (!organizationId || !name) return badRequest("Project name is required.");

  let project;
  try {
    project = await createProject(organizationId, name);
  } catch (error) {
    return handleVigilError(error);
  }

  // The project exists from here on, so this step must not fail the
  // request (a retry would create a duplicate project); the Evaluations
  // page shows the evaluator as not configured and can enable it.
  let relevanceEnabled = false;
  if (parsed.body.enableRelevance === true) {
    try {
      await vigilFetch("/v1/evaluations/configs/relevance", undefined, {
        method: "PUT",
        body: { enabled: true, sampling_rate: 1 },
        auth: { sessionToken: await requireSessionToken(), projectId: project.id },
      });
      relevanceEnabled = true;
    } catch {
      console.error("workspace: enabling relevance for a new project failed");
    }
  }

  const response = NextResponse.json({ ...project, relevanceEnabled }, { status: 201 });
  response.cookies.set(PROJECT_COOKIE_NAME, project.id, projectCookieOptions());
  return response;
}
