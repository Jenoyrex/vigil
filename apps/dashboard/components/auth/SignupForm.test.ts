import { describe, expect, it } from "vitest";

import { validateSignup } from "./SignupForm";

describe("validateSignup", () => {
  it("requires 12+ characters and a matching confirmation", () => {
    expect(validateSignup("short", "short")).toMatch(/12 characters/);
    expect(validateSignup("long enough password", "long enough passw0rd")).toMatch(/match/);
    expect(validateSignup("long enough password", "long enough password")).toBeNull();
  });
});
