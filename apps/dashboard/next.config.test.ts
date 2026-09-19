import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * `next.config.ts`'s `headers()` reads `process.env.NODE_ENV` once, at
 * module-evaluation time, to decide whether to include HSTS -- so each
 * test that cares about a specific environment sets `NODE_ENV`, resets
 * Vitest's module registry (`vi.resetModules()`), and re-imports with the
 * same static specifier. A dynamically-computed specifier (e.g. a
 * `?t=<timestamp>` cache-busting query string) doesn't work here: Vite
 * needs to statically analyze a dynamic `import()` call's specifier, and
 * rejects a fully runtime-computed one with "Unknown variable dynamic
 * import" -- confirmed by hitting exactly that error before switching to
 * this approach.
 */
async function loadHeaders(nodeEnv: string): Promise<{ key: string; value: string }[]> {
  // vi.stubEnv, not a direct process.env.NODE_ENV assignment: Next.js's
  // own type augmentation marks NODE_ENV read-only (a real `tsc`/`next
  // build` type-check failure, confirmed by hitting exactly that error
  // before switching to this), and vi.stubEnv is Vitest's own
  // purpose-built API for stubbing env vars in tests without fighting
  // that restriction.
  vi.stubEnv("NODE_ENV", nodeEnv);
  vi.resetModules();
  const mod = await import("./next.config");
  const config = mod.default;
  if (typeof config.headers !== "function") {
    throw new Error("expected next.config.ts's default export to have a headers() function");
  }
  const result = await config.headers();
  return result[0].headers;
}

describe("next.config.ts headers()", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("always includes the baseline headers, regardless of environment", async () => {
    const headers = await loadHeaders("development");
    const keys = headers.map((header) => header.key);
    expect(keys).toContain("X-Content-Type-Options");
    expect(keys).toContain("Referrer-Policy");
    expect(keys).toContain("Permissions-Policy");
    expect(keys).toContain("X-Frame-Options");
  });

  it("omits HSTS outside production (so `next dev` is never affected)", async () => {
    const headers = await loadHeaders("development");
    const keys = headers.map((header) => header.key);
    expect(keys).not.toContain("Strict-Transport-Security");
  });

  it("includes HSTS in production", async () => {
    const headers = await loadHeaders("production");
    const keys = headers.map((header) => header.key);
    expect(keys).toContain("Strict-Transport-Security");
  });

  it("never sets Content-Security-Policy, in any environment", async () => {
    // CSP lives in proxy.ts now, because it needs a fresh per-request nonce
    // -- see proxy.ts for the full policy and the reasoning. If this
    // static config ever set CSP again alongside proxy.ts's, the browser
    // would enforce the intersection of both headers, and a static
    // `script-src` with no matching nonce would silently block every
    // nonce'd script again (the exact bug this split fixes). This test is
    // a regression guard against that specific way of undoing the fix.
    const devHeaders = await loadHeaders("development");
    const prodHeaders = await loadHeaders("production");
    expect(devHeaders.map((header) => header.key)).not.toContain("Content-Security-Policy");
    expect(prodHeaders.map((header) => header.key)).not.toContain("Content-Security-Policy");
  });

  it("X-Frame-Options denies all framing", async () => {
    const headers = await loadHeaders("production");
    const value = headers.find((header) => header.key === "X-Frame-Options")?.value;
    expect(value).toBe("DENY");
  });

  it("HSTS is present but scoped to this host only (no includeSubDomains/preload)", async () => {
    // No includeSubDomains: this app is self-hosted under an
    // operator-chosen domain this codebase has no knowledge of, and
    // cannot know whether every sibling subdomain of it is HTTPS-only --
    // that domain-wide commitment belongs to the operator, not this app.
    // No preload, for the same reason one step further.
    const headers = await loadHeaders("production");
    const value = headers.find((header) => header.key === "Strict-Transport-Security")?.value ?? "";
    expect(value).toBe("max-age=15552000");
    expect(value).not.toContain("includeSubDomains");
    expect(value).not.toContain("preload");
  });
});
