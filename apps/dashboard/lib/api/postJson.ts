import { extractDetailMessage, type ApiErrorBody } from "./types";

/**
 * Browser-side POST of a JSON body to one of this app's own same-origin
 * routes (/api/auth/**, /api/workspace/**). Returns the parsed body, or
 * throws an Error carrying the server's `detail` message for display.
 */
export async function postJson<T>(path: string, body: unknown = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new Error("Unable to reach the server. Please retry.");
  }
  const data = (await response.json().catch(() => ({}))) as ApiErrorBody & T;
  if (!response.ok) {
    throw new Error(extractDetailMessage(data.detail) ?? "Something went wrong. Please retry.");
  }
  return data;
}
