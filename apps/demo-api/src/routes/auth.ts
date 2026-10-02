// /v1/auth/* -- mirrors apps/api/app/api/v1/auth.py and services/auth.py.

import { SESSION_TOKEN_HEADER, requireUser } from "../auth";
import { readJsonObject } from "../body";
import { type Env, RATE_LIMITS, SESSION_TTL_HOURS } from "../config";
import { ApiError, isoTime, json, noContent } from "../http";
import { PY_WHITESPACE, pyStrip } from "../pyjson";
import { clientIp, enforceRateLimit } from "../rateLimit";
import { DUMMY_PASSWORD_HASH, generateSessionToken, hashPassword, sha256Hex, verifyPassword } from "../security";
import { INVALID, type Obj, Validator, optionalStr, requiredStr } from "../validation";

// apps/api: re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$") with Python's unicode \s.
const EMAIL_PART = `[^@${PY_WHITESPACE}]+`;
const EMAIL_RE = new RegExp(`^${EMAIL_PART}@${EMAIL_PART}\\.${EMAIL_PART}$`, "u");

/** apps/api/app/schemas/auth.py's Email: strip + lower, then the pattern. */
function email(body: Obj, v: Validator) {
  const raw = requiredStr(body, "email", ["body"], v);
  if (raw === INVALID) return INVALID;
  const normalized = pyStrip(raw).toLowerCase();
  if (!EMAIL_RE.test(normalized)) return v.add("value_error", ["body", "email"], "Value error, must be a valid email address.");
  return normalized;
}

async function createSession(env: Env, userId: string): Promise<{ session_token: string; expires_at: string }> {
  const { rawToken, tokenHash } = await generateSessionToken();
  const now = Date.now();
  const expiresAt = now + SESSION_TTL_HOURS * 3600 * 1000;
  await env.DB.prepare(
    "INSERT INTO dashboard_sessions (id, user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
  )
    .bind(crypto.randomUUID(), userId, tokenHash, expiresAt, now)
    .run();
  return { session_token: rawToken, expires_at: isoTime(expiresAt) };
}

export async function signup(request: Request, env: Env): Promise<Response> {
  await enforceRateLimit(env, RATE_LIMITS.signupPerIp, clientIp(request, env));
  const body = await readJsonObject(request, 64 * 1024);
  const v = new Validator();
  const normalizedEmail = email(body, v);
  const password = requiredStr(body, "password", ["body"], v, { minLength: 12, maxLength: 200 });
  const fullName = optionalStr(body, "full_name", ["body"], v, { maxLength: 200 });
  v.throwIfInvalid();

  const existing = await env.DB.prepare("SELECT id FROM users WHERE email = ?").bind(normalizedEmail).first();
  if (existing) throw new ApiError(409, "An account with this email already exists. Log in instead.");
  const userId = crypto.randomUUID();
  const stripped = typeof fullName === "string" ? pyStrip(fullName) : "";
  try {
    await env.DB.prepare(
      "INSERT INTO users (id, email, full_name, hashed_password, created_at) VALUES (?, ?, ?, ?, ?)",
    )
      .bind(userId, normalizedEmail, stripped || null, await hashPassword(password as string), Date.now())
      .run();
  } catch (error) {
    if (String(error).includes("UNIQUE")) {
      throw new ApiError(409, "An account with this email already exists. Log in instead.");
    }
    throw error;
  }
  return json(await createSession(env, userId), 201);
}

export async function login(request: Request, env: Env): Promise<Response> {
  await enforceRateLimit(env, RATE_LIMITS.loginPerIp, clientIp(request, env));
  const body = await readJsonObject(request, 64 * 1024);
  const v = new Validator();
  const normalizedEmail = email(body, v);
  const password = requiredStr(body, "password", ["body"], v, { minLength: 1, maxLength: 200 });
  v.throwIfInvalid();
  await enforceRateLimit(env, RATE_LIMITS.loginPerAccount, normalizedEmail as string);

  const user = await env.DB.prepare("SELECT id, hashed_password, is_active FROM users WHERE email = ?")
    .bind(normalizedEmail)
    .first<{ id: string; hashed_password: string; is_active: number }>();
  const passwordOk = await verifyPassword(password as string, user?.hashed_password ?? DUMMY_PASSWORD_HASH);
  if (!user || !passwordOk || !user.is_active) throw new ApiError(401, "Invalid email or password.");
  return json(await createSession(env, user.id));
}

export async function session(request: Request, env: Env): Promise<Response> {
  const s = await requireUser(request, env);
  return json({ user_id: s.userId, email: s.email, expires_at: isoTime(s.expiresAt) });
}

export async function logout(request: Request, env: Env): Promise<Response> {
  const token = request.headers.get(SESSION_TOKEN_HEADER);
  if (token !== null) {
    await env.DB.prepare("UPDATE dashboard_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL")
      .bind(Date.now(), await sha256Hex(token))
      .run();
  }
  return noContent();
}
