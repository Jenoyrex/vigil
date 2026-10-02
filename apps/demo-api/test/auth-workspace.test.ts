import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import requestParity from "./fixtures/request-validation-parity.json";
import { ISO_UTC, PASSWORD, UUID, call, callJson, signup, unique } from "./helpers";

const BODY_ROUTES: Record<string, string> = {
  SignupRequest: "/v1/auth/signup",
  LoginRequest: "/v1/auth/login",
};

describe("request validation parity with apps/api (pydantic-generated fixtures)", () => {
  for (const c of requestParity.cases) {
    it(`${c.model} ${JSON.stringify(c.body).slice(0, 60)}`, async () => {
      let res;
      if (c.model === "NameRequest") {
        const { session } = await signup();
        res = await callJson("POST", "/v1/organizations", { session, body: c.body });
      } else {
        res = await callJson("POST", BODY_ROUTES[c.model], { body: c.body });
      }
      if ("errors" in c && c.errors) {
        expect(res.status).toBe(422);
        expect(res.body.detail.map((d: any) => ({ type: d.type, loc: d.loc, msg: d.msg }))).toEqual(c.errors);
      } else if (c.model === "NameRequest") {
        expect(res.status).toBe(201);
        expect(res.body.name).toBe((c as any).parsed.name);
      } else if (c.model === "SignupRequest") {
        expect(res.status).toBe(201);
        const session = await callJson("GET", "/v1/auth/session", { session: res.body.session_token });
        expect(session.body.email).toBe((c as any).parsed.email);
      }
    });
  }

  it("rejects malformed and missing JSON bodies like FastAPI", async () => {
    const bad = await callJson("POST", "/v1/auth/login", { rawBody: "{not json", headers: { "content-type": "application/json" } });
    expect(bad.status).toBe(422);
    expect(bad.body.detail[0]).toMatchObject({ type: "json_invalid", msg: "JSON decode error" });
    const missing = await callJson("POST", "/v1/auth/login", { rawBody: "" });
    expect(missing.body.detail).toEqual([{ type: "missing", loc: ["body"], msg: "Field required" }]);
    const array = await callJson("POST", "/v1/auth/login", { body: [1] });
    expect(array.body.detail[0].type).toBe("model_attributes_type");
  });
});

describe("auth: signup / login / session / logout", () => {
  it("signs up, normalizes the email, and returns a session", async () => {
    const email = `${unique()}@Example.COM`;
    const res = await callJson("POST", "/v1/auth/signup", { body: { email: `  ${email} `, password: PASSWORD, full_name: "  Ada  " } });
    expect(res.status).toBe(201);
    expect(Object.keys(res.body).sort()).toEqual(["expires_at", "session_token"]);
    expect(res.body.session_token).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(res.body.expires_at).toMatch(ISO_UTC);
    const hours = (Date.parse(res.body.expires_at) - Date.now()) / 3600e3;
    expect(hours).toBeGreaterThan(11.9);
    expect(hours).toBeLessThanOrEqual(12);

    const s = await callJson("GET", "/v1/auth/session", { session: res.body.session_token });
    expect(s.status).toBe(200);
    expect(Object.keys(s.body).sort()).toEqual(["email", "expires_at", "user_id"]);
    expect(s.body.email).toBe(email.toLowerCase());
    expect(s.body.user_id).toMatch(UUID);

    const me = await callJson("GET", "/v1/me", { session: res.body.session_token });
    expect(me.body).toEqual({ user: { id: s.body.user_id, email: email.toLowerCase(), full_name: "Ada" }, organizations: [] });
  });

  it("stores a demo PBKDF2 hash, never the password", async () => {
    const { email } = await signup();
    const row = await env.DB.prepare("SELECT hashed_password FROM users WHERE email = ?").bind(email).first<{ hashed_password: string }>();
    expect(row?.hashed_password).toMatch(/^pbkdf2_sha256\$8000\$/);
    expect(row?.hashed_password).not.toContain(PASSWORD);
  });

  it("rejects a duplicate email with 409, case-insensitively", async () => {
    const { email } = await signup();
    const res = await callJson("POST", "/v1/auth/signup", { body: { email: email.toUpperCase(), password: PASSWORD } });
    expect(res.status).toBe(409);
    expect(res.body).toEqual({ detail: "An account with this email already exists. Log in instead." });
  });

  it("logs in with correct credentials and gives the same generic 401 otherwise", async () => {
    const { email } = await signup();
    const ok = await callJson("POST", "/v1/auth/login", { body: { email: ` ${email.toUpperCase()}`, password: PASSWORD } });
    expect(ok.status).toBe(200);
    expect(ok.body.session_token).toBeTruthy();
    const wrong = await callJson("POST", "/v1/auth/login", { body: { email, password: "wrong password!!" } });
    const unknown = await callJson("POST", "/v1/auth/login", { body: { email: `${unique()}@nowhere.io`, password: PASSWORD } });
    for (const r of [wrong, unknown]) {
      expect(r.status).toBe(401);
      expect(r.body).toEqual({ detail: "Invalid email or password." });
    }
  });

  it("rejects missing, unknown, revoked and expired sessions with 401", async () => {
    const missing = await callJson("GET", "/v1/auth/session");
    const unknownToken = await callJson("GET", "/v1/auth/session", { session: "nope" });
    for (const r of [missing, unknownToken]) expect(r).toMatchObject({ status: 401, body: { detail: "Invalid or expired session." } });

    const { session } = await signup();
    const out = await call("POST", "/v1/auth/logout", { session });
    expect(out.status).toBe(204);
    expect(await out.text()).toBe("");
    expect((await callJson("GET", "/v1/auth/session", { session })).status).toBe(401);
    expect((await callJson("GET", "/v1/me", { session })).status).toBe(401);
    // Logout is idempotent: no token, unknown token, already revoked.
    for (const s of [undefined, "nope", session]) expect((await call("POST", "/v1/auth/logout", { session: s })).status).toBe(204);

    const second = await signup();
    await env.DB.prepare("UPDATE dashboard_sessions SET expires_at = ?").bind(Date.now() - 1).run();
    expect((await callJson("GET", "/v1/auth/session", { session: second.session })).status).toBe(401);
  });
});

describe("workspace: organizations, projects, API keys", () => {
  it("creates an organization and project and lists them in /v1/me", async () => {
    const { session } = await signup();
    const org = await callJson("POST", "/v1/organizations", { session, body: { name: "  Acme Corp!!  " } });
    expect(org.status).toBe(201);
    expect(org.body).toMatchObject({ name: "Acme Corp!!", role: "owner", projects: [] });
    expect(org.body.slug).toMatch(/^acme-corp-[0-9a-f]{8}$/);
    const project = await callJson("POST", `/v1/organizations/${org.body.id}/projects`, { session, body: { name: "Web" } });
    expect(project.status).toBe(201);
    expect(Object.keys(project.body).sort()).toEqual(["created_at", "id", "name", "slug"]);
    expect(project.body.created_at).toMatch(ISO_UTC);
    const me = await callJson("GET", "/v1/me", { session });
    expect(me.body.organizations).toEqual([{ ...org.body, projects: [project.body] }]);
  });

  it("uses 'workspace' for a name with no slug characters", async () => {
    const { session } = await signup();
    const org = await callJson("POST", "/v1/organizations", { session, body: { name: "東京" } });
    expect(org.body.slug).toMatch(/^workspace-[0-9a-f]{8}$/);
  });

  it("creates, lists and revokes API keys; the raw key is shown once", async () => {
    const { session } = await signup();
    const org = await callJson("POST", "/v1/organizations", { session, body: { name: "A" } });
    const project = await callJson("POST", `/v1/organizations/${org.body.id}/projects`, { session, body: { name: "P" } });
    const created = await callJson("POST", `/v1/projects/${project.body.id}/api-keys`, { session, body: { name: " ci " } });
    expect(created.status).toBe(201);
    expect(created.body.api_key).toMatch(/^vgl_[0-9a-f]{12}\.[A-Za-z0-9_-]{43}$/);
    expect(created.body.api_key.startsWith(`${created.body.key_prefix}.`)).toBe(true);
    expect(created.body).toMatchObject({ name: "ci", status: "active", last_used_at: null, revoked_at: null });
    expect(Object.keys(created.body).sort()).toEqual(
      ["api_key", "created_at", "id", "key_prefix", "last_used_at", "name", "revoked_at", "status"],
    );

    const list = await callJson("GET", `/v1/projects/${project.body.id}/api-keys`, { session });
    expect(list.body.items).toHaveLength(1);
    expect(list.body.items[0]).not.toHaveProperty("api_key");

    const revoked = await callJson("POST", `/v1/projects/${project.body.id}/api-keys/${created.body.id}/revoke`, { session });
    expect(revoked.status).toBe(200);
    expect(revoked.body.status).toBe("revoked");
    expect(revoked.body.revoked_at).toMatch(ISO_UTC);
    const again = await callJson("POST", `/v1/projects/${project.body.id}/api-keys/${created.body.id}/revoke`, { session });
    expect(again.body.revoked_at).toBe(revoked.body.revoked_at);
  });

  it("isolates tenants: another user's org, project and keys are 404", async () => {
    const alice = await signup();
    const bob = await signup();
    const org = await callJson("POST", "/v1/organizations", { session: alice.session, body: { name: "A" } });
    const project = await callJson("POST", `/v1/organizations/${org.body.id}/projects`, { session: alice.session, body: { name: "P" } });
    const key = await callJson("POST", `/v1/projects/${project.body.id}/api-keys`, { session: alice.session, body: { name: "k" } });
    const attempts = [
      callJson("POST", `/v1/organizations/${org.body.id}/projects`, { session: bob.session, body: { name: "X" } }),
      callJson("GET", `/v1/projects/${project.body.id}/api-keys`, { session: bob.session }),
      callJson("POST", `/v1/projects/${project.body.id}/api-keys`, { session: bob.session, body: { name: "x" } }),
      callJson("POST", `/v1/projects/${project.body.id}/api-keys/${key.body.id}/revoke`, { session: bob.session }),
    ];
    for (const r of await Promise.all(attempts)) expect(r).toMatchObject({ status: 404, body: { detail: "Not found." } });
    expect((await callJson("GET", "/v1/me", { session: bob.session })).body.organizations).toEqual([]);
  });

  it("forbids non-manager members from managing projects and keys (403)", async () => {
    const owner = await signup();
    const member = await signup();
    const org = await callJson("POST", "/v1/organizations", { session: owner.session, body: { name: "A" } });
    const project = await callJson("POST", `/v1/organizations/${org.body.id}/projects`, { session: owner.session, body: { name: "P" } });
    const memberId = (await callJson("GET", "/v1/auth/session", { session: member.session })).body.user_id;
    await env.DB.prepare("INSERT INTO organization_memberships (id, user_id, organization_id, role, created_at) VALUES (?, ?, ?, 'member', 0)")
      .bind(crypto.randomUUID(), memberId, org.body.id)
      .run();
    const forbidden = { status: 403, body: { detail: "Only organization owners and admins can do this." } };
    expect(await callJson("POST", `/v1/organizations/${org.body.id}/projects`, { session: member.session, body: { name: "X" } })).toMatchObject(forbidden);
    expect(await callJson("POST", `/v1/projects/${project.body.id}/api-keys`, { session: member.session, body: { name: "x" } })).toMatchObject(forbidden);
    expect((await callJson("GET", `/v1/projects/${project.body.id}/api-keys`, { session: member.session })).status).toBe(200);
  });

  it("checks auth before validating path parameters (FastAPI dependency order)", async () => {
    expect((await callJson("POST", "/v1/organizations/not-a-uuid/projects", { body: { name: "x" } })).status).toBe(401);
    const { session } = await signup();
    const bad = await callJson("POST", "/v1/organizations/not-a-uuid/projects", { session, body: { name: "x" } });
    expect(bad.status).toBe(422);
    expect(bad.body.detail[0]).toMatchObject({ type: "uuid_parsing", loc: ["path", "organization_id"] });
  });

  it("enforces demo caps on organizations, projects and API keys", async () => {
    const { session } = await signup();
    const orgs = [];
    for (let i = 0; i < 3; i++) orgs.push(await callJson("POST", "/v1/organizations", { session, body: { name: `o${i}` } }));
    const fourth = await callJson("POST", "/v1/organizations", { session, body: { name: "o4" } });
    expect(fourth.status).toBe(403);
    expect(fourth.body.detail).toMatch(/^Public demo limit reached: at most 3 organizations per account/);

    const orgId = orgs[0].body.id;
    for (let i = 0; i < 5; i++) expect((await callJson("POST", `/v1/organizations/${orgId}/projects`, { session, body: { name: `p${i}` } })).status).toBe(201);
    expect((await callJson("POST", `/v1/organizations/${orgId}/projects`, { session, body: { name: "p6" } })).status).toBe(403);
  });
});

describe("routing", () => {
  it("returns FastAPI-style 404 and 405", async () => {
    expect(await callJson("GET", "/v1/nope")).toMatchObject({ status: 404, body: { detail: "Not Found" } });
    const res = await callJson("GET", "/v1/auth/login");
    expect(res).toMatchObject({ status: 405, body: { detail: "Method Not Allowed" } });
    expect(res.response.headers.get("allow")).toBe("POST");
  });
});
