// Response and error conventions of apps/api (FastAPI), reproduced exactly:
// errors are `{"detail": string}` or, for request validation, `{"detail":
// [{type, loc, msg, input?}]}` with status 422.

export type Loc = (string | number)[];

export interface ValidationItem {
  type: string;
  loc: Loc;
  msg: string;
  input?: unknown;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string | ValidationItem[],
    readonly headers: Record<string, string> = {},
  ) {
    super(typeof detail === "string" ? detail : detail.map((d) => d.msg).join("; "));
  }
}

export function validationError(items: ValidationItem[]): ApiError {
  return new ApiError(422, items);
}

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

export function noContent(): Response {
  return new Response(null, { status: 204 });
}

export function errorResponse(error: ApiError): Response {
  return json({ detail: error.detail }, error.status, error.headers);
}

export const notFound = (detail = "Not found.") => new ApiError(404, detail);

/**
 * Pydantic v2's JSON form of a UTC datetime: `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`
 * (the fraction is omitted when zero). Storage precision is milliseconds.
 */
export function isoTime(ms: number): string {
  const iso = new Date(ms).toISOString(); // YYYY-MM-DDTHH:MM:SS.mmmZ
  const millis = iso.slice(20, 23);
  return millis === "000" ? `${iso.slice(0, 19)}Z` : `${iso.slice(0, 19)}.${millis}000Z`;
}

export function isoTimeOrNull(ms: number | null): string | null {
  return ms === null ? null : isoTime(ms);
}

/** Python `datetime.isoformat()` of a UTC-aware datetime (used inside cursors). */
export function pyIsoformat(ms: number): string {
  const iso = new Date(ms).toISOString();
  const millis = iso.slice(20, 23);
  return millis === "000" ? `${iso.slice(0, 19)}+00:00` : `${iso.slice(0, 19)}.${millis}000+00:00`;
}

export async function readBodyText(request: Request, maxBytes: number): Promise<string> {
  const declared = Number(request.headers.get("content-length") ?? "0");
  const tooLarge = () =>
    new ApiError(413, `Request body exceeds the maximum allowed size of ${maxBytes} bytes.`);
  if (declared > maxBytes) throw tooLarge();
  const buffer = await request.arrayBuffer();
  if (buffer.byteLength > maxBytes) throw tooLarge();
  return new TextDecoder().decode(buffer);
}
