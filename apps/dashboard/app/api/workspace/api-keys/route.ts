import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { createApiKey, listApiKeys, requireCurrentProject } from "@/lib/api/workspace";

import { handleVigilError } from "../../vigil/_lib/handleError";
import { badRequest, readJson, requiredString } from "../_lib";

/** API keys of the project currently being viewed. */
export async function GET(): Promise<NextResponse> {
  try {
    const { currentProject } = await requireCurrentProject();
    return NextResponse.json(await listApiKeys(currentProject.id));
  } catch (error) {
    return handleVigilError(error);
  }
}

/** Creates a key; the response carries the raw key -- the only time it's ever shown. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const parsed = await readJson(request);
  if ("error" in parsed) return parsed.error;
  const name = requiredString(parsed.body, "name");
  if (!name) return badRequest("Key name is required.");
  try {
    const { currentProject } = await requireCurrentProject();
    const created = await createApiKey(currentProject.id, name);
    return NextResponse.json(created, { status: 201, headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return handleVigilError(error);
  }
}
