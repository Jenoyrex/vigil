import { describe, expect, it } from "vitest";

import { bucketForPreset, isTimeRangePreset, resolveTimeRange } from "./time-range";

describe("isTimeRangePreset", () => {
  it("accepts the three known presets", () => {
    expect(isTimeRangePreset("1h")).toBe(true);
    expect(isTimeRangePreset("24h")).toBe(true);
    expect(isTimeRangePreset("7d")).toBe(true);
  });

  it("rejects anything else, including null/undefined", () => {
    expect(isTimeRangePreset("30d")).toBe(false);
    expect(isTimeRangePreset(null)).toBe(false);
    expect(isTimeRangePreset(undefined)).toBe(false);
  });
});

describe("resolveTimeRange", () => {
  const now = new Date("2026-09-01T12:00:00.000Z");

  it("resolves 1h to a one-hour window ending at now", () => {
    const range = resolveTimeRange("1h", now);
    expect(range.start_time_to).toBe(now.toISOString());
    expect(range.start_time_from).toBe("2026-09-01T11:00:00.000Z");
  });

  it("resolves 24h to a 24-hour window", () => {
    const range = resolveTimeRange("24h", now);
    expect(range.start_time_from).toBe("2026-08-31T12:00:00.000Z");
  });

  it("resolves 7d to a 7-day window matching the API's max window", () => {
    const range = resolveTimeRange("7d", now);
    expect(range.start_time_from).toBe("2026-08-25T12:00:00.000Z");
  });
});

describe("bucketForPreset", () => {
  it("uses hourly buckets for 1h and 24h", () => {
    expect(bucketForPreset("1h")).toBe("hour");
    expect(bucketForPreset("24h")).toBe("hour");
  });

  it("uses daily buckets for 7d, to keep the chart readable", () => {
    expect(bucketForPreset("7d")).toBe("day");
  });
});
