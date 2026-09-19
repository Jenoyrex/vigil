import { describe, expect, it } from "vitest";

import { sanitizeNextPath } from "./page";

describe("sanitizeNextPath", () => {
  it("preserves a same-origin path with its full query string", () => {
    expect(sanitizeNextPath("/traces?status=failed&cursor=abc")).toBe(
      "/traces?status=failed&cursor=abc",
    );
  });

  it("falls back to / when the value is missing", () => {
    expect(sanitizeNextPath(undefined)).toBe("/");
  });

  it("falls back to / for a value that doesn't start with /", () => {
    expect(sanitizeNextPath("traces")).toBe("/");
  });

  it("rejects a protocol-relative URL (//host/path)", () => {
    expect(sanitizeNextPath("//evil.example.com/traces")).toBe("/");
  });

  it("rejects an absolute URL to another origin", () => {
    expect(sanitizeNextPath("https://evil.example.com/traces")).toBe("/");
  });

  it("rejects a backslash-based //-equivalent bypass (/\\evil.example.com)", () => {
    expect(sanitizeNextPath("/\\evil.example.com")).toBe("/");
    expect(sanitizeNextPath("/\\/evil.example.com")).toBe("/");
  });
});
