// Token, key and password primitives (WebCrypto only).

const encoder = new TextEncoder();

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

function fromBase64(text: string): Uint8Array {
  return Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
}

export function randomHex(byteCount: number): string {
  return bytesToHex(crypto.getRandomValues(new Uint8Array(byteCount)));
}

/** Python `secrets.token_urlsafe(n)`. */
function tokenUrlsafe(byteCount: number): string {
  return base64url(crypto.getRandomValues(new Uint8Array(byteCount)));
}

export async function sha256Hex(text: string): Promise<string> {
  return bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(text))));
}

export async function sha256Bytes(text: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(text)));
}

// ---- API keys: identical format and hashing to apps/api/app/security/api_keys.py

const API_KEY_PREFIX_SCHEME = "vgl_";

export async function generateApiKey(): Promise<{ rawKey: string; keyPrefix: string; keyHash: string }> {
  const keyPrefix = `${API_KEY_PREFIX_SCHEME}${randomHex(6)}`;
  const rawKey = `${keyPrefix}.${tokenUrlsafe(32)}`;
  return { rawKey, keyPrefix, keyHash: await sha256Hex(rawKey) };
}

export function hasExpectedKeyShape(rawKey: string): boolean {
  const dot = rawKey.indexOf(".");
  return dot !== -1 && rawKey.slice(0, dot).startsWith(API_KEY_PREFIX_SCHEME) && dot < rawKey.length - 1;
}

// ---- Sessions: identical to apps/api/app/security/sessions.py

export async function generateSessionToken(): Promise<{ rawToken: string; tokenHash: string }> {
  const rawToken = tokenUrlsafe(32);
  return { rawToken, tokenHash: await sha256Hex(rawToken) };
}

// ---- Passwords: DEMO-ONLY. Production uses scrypt (N=2^14, r=8), which does
// not fit the Workers free plan's 10 ms CPU budget; see ADR 009. Format:
// pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>. The iteration count is
// stored per hash so it can be retuned without invalidating accounts.

const PBKDF2_ITERATIONS = 8000;

async function pbkdf2(password: string, salt: Uint8Array, iterations: number): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, key, 256);
  return new Uint8Array(bits);
}

export async function hashPassword(password: string, iterations = PBKDF2_ITERATIONS): Promise<string> {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const hash = await pbkdf2(password, salt, iterations);
  return `pbkdf2_sha256$${iterations}$${base64(salt)}$${base64(hash)}`;
}

function timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

export async function verifyPassword(password: string, encoded: string): Promise<boolean> {
  const [scheme, iterationsText, saltB64, hashB64] = encoded.split("$");
  const iterations = Number(iterationsText);
  if (scheme !== "pbkdf2_sha256" || !Number.isInteger(iterations) || iterations < 1) return false;
  const expected = fromBase64(hashB64);
  return timingSafeEqual(await pbkdf2(password, fromBase64(saltB64), iterations), expected);
}

/** Verified against when the email is unknown, so timing does not reveal it. */
export const DUMMY_PASSWORD_HASH =
  `pbkdf2_sha256$${PBKDF2_ITERATIONS}$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=`;

export function constantTimeStringEqual(a: string, b: string): boolean {
  return timingSafeEqual(encoder.encode(a), encoder.encode(b));
}
