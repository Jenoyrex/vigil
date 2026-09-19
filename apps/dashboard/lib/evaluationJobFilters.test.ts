import { describe, expect, it } from "vitest";

import {
  DEFAULT_EVALUATION_JOB_FILTERS,
  evaluationJobFiltersToSearchParams,
  searchParamsToEvaluationJobFilters,
  withoutCursor,
  type EvaluationJobFilters,
} from "./evaluationJobFilters";

describe("evaluationJobFiltersToSearchParams", () => {
  it("omits every filter when none are set", () => {
    const params = evaluationJobFiltersToSearchParams({});
    expect(params.toString()).toBe("");
  });

  it("includes every set filter", () => {
    const params = evaluationJobFiltersToSearchParams({
      status: "failed",
      evaluatorName: "relevance",
      cursor: "abc123",
    });
    expect(params.get("status")).toBe("failed");
    expect(params.get("evaluator_name")).toBe("relevance");
    expect(params.get("cursor")).toBe("abc123");
  });
});

describe("searchParamsToEvaluationJobFilters", () => {
  it("round-trips a full filter set through URLSearchParams", () => {
    const original: EvaluationJobFilters = {
      status: "dead_letter",
      evaluatorName: "relevance_embedding",
      cursor: "xyz",
    };
    const roundTripped = searchParamsToEvaluationJobFilters(evaluationJobFiltersToSearchParams(original));
    expect(roundTripped).toEqual(original);
  });

  it("falls back to no status filter for a missing or invalid status param", () => {
    expect(searchParamsToEvaluationJobFilters(new URLSearchParams()).status).toBeUndefined();
    expect(searchParamsToEvaluationJobFilters(new URLSearchParams("status=not-a-real-status")).status).toBeUndefined();
  });

  it("accepts every one of the five real job statuses", () => {
    for (const status of ["pending", "running", "succeeded", "failed", "dead_letter"] as const) {
      expect(searchParamsToEvaluationJobFilters(new URLSearchParams(`status=${status}`)).status).toBe(status);
    }
  });

  it("also accepts a Next.js-style plain search params record", () => {
    const filters = searchParamsToEvaluationJobFilters({ status: "pending", evaluator_name: "relevance" });
    expect(filters.status).toBe("pending");
    expect(filters.evaluatorName).toBe("relevance");
  });

  it("takes the first value when a key appears as an array (Next.js multi-value params)", () => {
    const filters = searchParamsToEvaluationJobFilters({ status: ["pending", "failed"] });
    expect(filters.status).toBe("pending");
  });

  it("matches DEFAULT_EVALUATION_JOB_FILTERS for an empty params set", () => {
    expect(searchParamsToEvaluationJobFilters(new URLSearchParams())).toEqual(DEFAULT_EVALUATION_JOB_FILTERS);
  });
});

describe("withoutCursor", () => {
  it("removes the cursor while preserving every other filter", () => {
    const filters: EvaluationJobFilters = { status: "failed", evaluatorName: "relevance", cursor: "abc" };
    const result = withoutCursor(filters);
    expect(result.cursor).toBeUndefined();
    expect(result.status).toBe("failed");
    expect(result.evaluatorName).toBe("relevance");
  });

  it("does not mutate the input", () => {
    const filters: EvaluationJobFilters = { status: "pending", cursor: "abc" };
    withoutCursor(filters);
    expect(filters.cursor).toBe("abc");
  });
});
