import { describe, expect, it } from "vitest";

import { latestTraceWindow, TRACE_WINDOW_MS } from "./useLatestTrace";

describe("latestTraceWindow", () => {
  it("never exceeds GET /v1/traces' 7-day maximum window, skew included", () => {
    const now = new Date("2026-09-27T12:00:00Z");
    const { start_time_from, start_time_to } = latestTraceWindow(now);
    expect(Date.parse(start_time_to) - Date.parse(start_time_from)).toBeLessThanOrEqual(TRACE_WINDOW_MS);
    expect(Date.parse(start_time_to)).toBeGreaterThan(now.getTime());
  });
});
