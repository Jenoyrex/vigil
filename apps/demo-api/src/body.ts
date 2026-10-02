import { readBodyText, validationError } from "./http";
import { parseJsonPreservingNumbers } from "./pyjson";
import { type Obj, Validator, requireObjectBody } from "./validation";

/** Parse a JSON object body with FastAPI's error shapes (missing/invalid JSON). */
export function parseJsonObject(text: string): Obj {
  if (text.length === 0) throw validationError([{ type: "missing", loc: ["body"], msg: "Field required" }]);
  let parsed: unknown;
  try {
    parsed = parseJsonPreservingNumbers(text);
  } catch {
    throw validationError([{ type: "json_invalid", loc: ["body", 0], msg: "JSON decode error" }]);
  }
  return requireObjectBody(parsed, new Validator());
}

export async function readJsonObject(request: Request, maxBytes: number): Promise<Obj> {
  return parseJsonObject(await readBodyText(request, maxBytes));
}
