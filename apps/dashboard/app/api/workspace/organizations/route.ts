import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { createOrganization } from "@/lib/api/workspace";

import { handleVigilError } from "../../vigil/_lib/handleError";
import { badRequest, readJson, requiredString } from "../_lib";

/** POST /api/workspace/organizations -- the signed-in user becomes its owner. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const parsed = await readJson(request);
  if ("error" in parsed) return parsed.error;
  const name = requiredString(parsed.body, "name");
  if (!name) return badRequest("Organization name is required.");
  try {
    return NextResponse.json(await createOrganization(name), { status: 201 });
  } catch (error) {
    return handleVigilError(error);
  }
}
