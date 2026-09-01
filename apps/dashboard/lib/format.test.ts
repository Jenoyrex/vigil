import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatCost,
  formatCount,
  formatDuration,
  formatNullableCount,
  formatPercent,
  truncateId,
} from "./format";

describe("formatDuration", () => {
  it("formats sub-second durations in milliseconds", () => {
    expect(formatDuration(0)).toBe("0ms");
    expect(formatDuration(245)).toBe("245ms");
    expect(formatDuration(999)).toBe("999ms");
  });

  it("formats seconds with one decimal place", () => {
    expect(formatDuration(1000)).toBe("1s");
    expect(formatDuration(12500)).toBe("12.5s");
  });

  it("formats minutes once seconds would exceed 60", () => {
    expect(formatDuration(90_000)).toBe("1.5m");
  });

  it("formats hours once minutes would exceed 60", () => {
    expect(formatDuration(2 * 60 * 60 * 1000)).toBe("2h");
  });

  it("returns an em dash for non-finite input", () => {
    expect(formatDuration(NaN)).toBe("—");
  });
});

describe("formatBytes", () => {
  it("formats sub-KB sizes in bytes", () => {
    expect(formatBytes(500)).toBe("500 B");
  });

  it("formats KB with one decimal place", () => {
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
  });

  it("formats MB once KB would exceed 1024", () => {
    expect(formatBytes(1024 * 1024 * 2)).toBe("2 MB");
  });
});

describe("formatCount / formatNullableCount", () => {
  it("adds thousands separators", () => {
    expect(formatCount(1234567)).toBe("1,234,567");
  });

  it("renders null as an em dash", () => {
    expect(formatNullableCount(null)).toBe("—");
    expect(formatNullableCount(42)).toBe("42");
  });
});

describe("formatPercent", () => {
  it("converts a fraction to a percentage string", () => {
    expect(formatPercent(0.0234)).toBe("2.34%");
    expect(formatPercent(0)).toBe("0.00%");
    expect(formatPercent(1)).toBe("100.00%");
  });
});

describe("formatCost", () => {
  it("renders null as an em dash", () => {
    expect(formatCost(null)).toBe("—");
  });

  it("preserves small fractional-cent precision without float rounding", () => {
    expect(formatCost("0.000340")).toBe("$0.00034");
  });

  it("trims trailing zeros but keeps at least two decimal places", () => {
    expect(formatCost("1.500000")).toBe("$1.50");
    expect(formatCost("0.000000")).toBe("$0.00");
  });

  it("adds thousands separators to the whole-number part", () => {
    expect(formatCost("12345.678900")).toBe("$12,345.6789");
  });

  it("preserves a negative sign", () => {
    expect(formatCost("-1.500000")).toBe("-$1.50");
  });

  it("never round-trips through Number() -- exact string echoing for a value at the edge of float precision", () => {
    // 0.1 + 0.2 !== 0.3 in IEEE-754; formatCost must not care, since it
    // never performs floating-point arithmetic on the input at all.
    const value = "999999999999.999999";
    expect(formatCost(value)).toBe("$999,999,999,999.999999");
  });
});

describe("truncateId", () => {
  it("shortens a long id to head…tail", () => {
    expect(truncateId("4bf92f3577b34da6a3ce929d0e0e4736")).toBe("4bf92f35…4736");
  });

  it("leaves a short id unchanged", () => {
    expect(truncateId("abc123")).toBe("abc123");
  });
});
