import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { requireCurrentProject, revokeApiKey } from "@/lib/api/workspace";

import { handleVigilError } from "../../../../vigil/_lib/handleError";
import { readJson } from "../../../_lib";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ keyId: string }> },
): Promise<NextResponse> {
  const parsed = await readJson(request);
  if ("error" in parsed) return parsed.error;
  const { keyId } = await params;
  try {
    const { currentProject } = await requireCurrentProject();
    return NextResponse.json(await revokeApiKey(currentProject.id, keyId));
  } catch (error) {
    return handleVigilError(error);
  }
}
