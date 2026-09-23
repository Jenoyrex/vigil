import { describe, expect, it } from "vitest";

import { deriveClientIp, parseTrustedProxyHops } from "./clientIp";

describe("parseTrustedProxyHops", () => {
  it("defaults to 0 when unset or empty", () => {
    expect(parseTrustedProxyHops(undefined)).toBe(0);
    expect(parseTrustedProxyHops("")).toBe(0);
  });

  it("parses a plain non-negative integer", () => {
    expect(parseTrustedProxyHops("0")).toBe(0);
    expect(parseTrustedProxyHops("1")).toBe(1);
    expect(parseTrustedProxyHops(" 2 ")).toBe(2);
  });

  it("treats anything else as 0 (no trusted proxy)", () => {
    for (const bad of ["-1", "1.5", "one", "1e3", "+1", "1,2"]) {
      expect(parseTrustedProxyHops(bad)).toBe(0);
    }
  });
});

describe("deriveClientIp", () => {
  it("returns null with no trusted proxy, however plausible the header looks", () => {
    expect(deriveClientIp("203.0.113.7", 0)).toBeNull();
    expect(deriveClientIp("198.51.100.1, 203.0.113.7", 0)).toBeNull();
  });

  it("returns null when there is no header", () => {
    expect(deriveClientIp(null, 1)).toBeNull();
    expect(deriveClientIp("", 1)).toBeNull();
  });

  it("takes the entry the single trusted proxy appended (rightmost)", () => {
    expect(deriveClientIp("203.0.113.7", 1)).toBe("203.0.113.7");
  });

  it("ignores client-supplied entries to the left of the trusted proxy's", () => {
    // A spoofing client put 1.2.3.4 in the header; the trusted proxy
    // appended the address it really saw.
    expect(deriveClientIp("1.2.3.4, 203.0.113.7", 1)).toBe("203.0.113.7");
  });

  it("counts hops from the right for multiple trusted proxies", () => {
    expect(deriveClientIp("1.2.3.4, 203.0.113.7, 10.0.0.5", 2)).toBe("203.0.113.7");
  });

  it("returns null when fewer entries exist than trusted hops", () => {
    expect(deriveClientIp("203.0.113.7", 2)).toBeNull();
  });

  it("accepts IPv6", () => {
    expect(deriveClientIp("2001:db8::1", 1)).toBe("2001:db8::1");
  });

  it("returns null for a malformed candidate", () => {
    expect(deriveClientIp("not-an-ip", 1)).toBeNull();
    expect(deriveClientIp("203.0.113.7, evil\r\nX-Injected: 1", 1)).toBeNull();
    expect(deriveClientIp("203.0.113.7, 999.1.1.1", 1)).toBeNull();
  });
});
