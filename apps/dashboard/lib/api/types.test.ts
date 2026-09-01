import { describe, expect, it } from "vitest";

import { extractDetailMessage } from "./types";

describe("extractDetailMessage", () => {
  it("passes through a plain string detail unchanged", () => {
    // The shape of an explicit HTTPException(detail="...") from the API,
    // e.g. app/services/query.py's QueryValidationError.
    expect(extractDetailMessage("Trace not found.")).toBe("Trace not found.");
  });

  it("extracts a readable message from FastAPI's automatic validation-error array", () => {
    // The shape FastAPI itself produces for a Pydantic AfterValidator
    // failure (e.g. a malformed trace_id path parameter) -- this is what
    // was rendering as the literal string "[object Object]" before this
    // function existed.
    const detail = [
      {
        type: "value_error",
        loc: ["path", "trace_id"],
        msg: "Value error, trace_id must be exactly 32 hexadecimal characters",
      },
    ];
    expect(extractDetailMessage(detail)).toBe(
      "Value error, trace_id must be exactly 32 hexadecimal characters",
    );
  });

  it("joins multiple validation error messages", () => {
    const detail = [{ msg: "field a is required" }, { msg: "field b must be positive" }];
    expect(extractDetailMessage(detail)).toBe("field a is required; field b must be positive");
  });

  it("returns undefined for an empty array", () => {
    expect(extractDetailMessage([])).toBeUndefined();
  });

  it("returns undefined for missing/undefined detail", () => {
    expect(extractDetailMessage(undefined)).toBeUndefined();
  });

  it("skips array items with no usable msg field rather than rendering '[object Object]'", () => {
    const detail = [{ loc: ["body"] }, { msg: "the only real message" }];
    expect(extractDetailMessage(detail)).toBe("the only real message");
  });
});
