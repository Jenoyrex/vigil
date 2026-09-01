import { describe, expect, it } from "vitest";

import { buildQueryString } from "./queryString";

describe("buildQueryString", () => {
  it("returns an empty string for undefined or empty params", () => {
    expect(buildQueryString(undefined)).toBe("");
    expect(buildQueryString({})).toBe("");
  });

  it("omits keys whose value is undefined", () => {
    expect(buildQueryString({ environment: undefined, resource: "checkout" })).toBe("?resource=checkout");
  });

  it("includes boolean and number values, stringified", () => {
    const query = buildQueryString({ has_error: true, limit: 20 });
    const params = new URLSearchParams(query.slice(1));
    expect(params.get("has_error")).toBe("true");
    expect(params.get("limit")).toBe("20");
  });

  it("includes false and zero, distinguishing them from omitted (undefined)", () => {
    const query = buildQueryString({ has_error: false, limit: 0 });
    const params = new URLSearchParams(query.slice(1));
    expect(params.get("has_error")).toBe("false");
    expect(params.get("limit")).toBe("0");
  });

  it("percent-encodes values that need it", () => {
    const query = buildQueryString({ resource: "a b/c" });
    expect(query).toContain("resource=a+b%2Fc");
  });

  it("prefixes a non-empty query string with '?'", () => {
    expect(buildQueryString({ environment: "production" })).toBe("?environment=production");
  });
});
