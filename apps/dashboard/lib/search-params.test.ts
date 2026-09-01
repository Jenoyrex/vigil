import { describe, expect, it } from "vitest";

import {
  DEFAULT_TRACE_FILTERS,
  searchParamsToTraceFilters,
  traceFiltersToSearchParams,
  withoutCursor,
  type TraceFilters,
} from "./search-params";

describe("traceFiltersToSearchParams", () => {
  it("always includes range, and omits unset optional filters", () => {
    const params = traceFiltersToSearchParams({ range: "24h" });
    expect(params.get("range")).toBe("24h");
    expect(params.has("environment")).toBe(false);
    expect(params.has("resource")).toBe(false);
    expect(params.has("has_error")).toBe(false);
    expect(params.has("cursor")).toBe(false);
  });

  it("includes every set filter", () => {
    const params = traceFiltersToSearchParams({
      range: "7d",
      environment: "production",
      resource: "checkout-service",
      hasError: true,
      cursor: "abc123",
    });
    expect(params.get("environment")).toBe("production");
    expect(params.get("resource")).toBe("checkout-service");
    expect(params.get("has_error")).toBe("true");
    expect(params.get("cursor")).toBe("abc123");
  });

  it("distinguishes hasError=false from unset (omitted)", () => {
    const params = traceFiltersToSearchParams({ range: "24h", hasError: false });
    expect(params.get("has_error")).toBe("false");
  });
});

describe("searchParamsToTraceFilters", () => {
  it("round-trips a full filter set through URLSearchParams", () => {
    const original: TraceFilters = {
      range: "7d",
      environment: "production",
      resource: "checkout-service",
      hasError: false,
      cursor: "xyz",
    };
    const roundTripped = searchParamsToTraceFilters(traceFiltersToSearchParams(original));
    expect(roundTripped).toEqual(original);
  });

  it("falls back to the default range for a missing or invalid range param", () => {
    expect(searchParamsToTraceFilters(new URLSearchParams()).range).toBe(DEFAULT_TRACE_FILTERS.range);
    expect(searchParamsToTraceFilters(new URLSearchParams("range=30d")).range).toBe(
      DEFAULT_TRACE_FILTERS.range,
    );
  });

  it("leaves hasError undefined when has_error is absent", () => {
    expect(searchParamsToTraceFilters(new URLSearchParams("range=24h")).hasError).toBeUndefined();
  });

  it("also accepts a Next.js-style plain search params record", () => {
    const filters = searchParamsToTraceFilters({ range: "1h", environment: "staging" });
    expect(filters.range).toBe("1h");
    expect(filters.environment).toBe("staging");
  });

  it("takes the first value when a key appears as an array (Next.js multi-value params)", () => {
    const filters = searchParamsToTraceFilters({ range: ["7d", "1h"] });
    expect(filters.range).toBe("7d");
  });
});

describe("withoutCursor", () => {
  it("removes the cursor while preserving every other filter", () => {
    const filters: TraceFilters = { range: "24h", environment: "production", cursor: "abc" };
    const result = withoutCursor(filters);
    expect(result.cursor).toBeUndefined();
    expect(result.range).toBe("24h");
    expect(result.environment).toBe("production");
  });

  it("does not mutate the input", () => {
    const filters: TraceFilters = { range: "24h", cursor: "abc" };
    withoutCursor(filters);
    expect(filters.cursor).toBe("abc");
  });
});
