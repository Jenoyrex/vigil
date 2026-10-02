import { describe, expect, it } from "vitest";
import { parseJsonPreservingNumbers, pyFloatRepr, pyJsonDumps, pyLen, pyStrip } from "../src/pyjson";
import { INVALID, Validator, coerceDatetime, parseUuid } from "../src/validation";
import formats from "./fixtures/python-format-parity.json";

describe("Python formatting parity (fixtures from CPython)", () => {
  // JSON cannot carry -0.0; the fixture's second value is Python's -0.0.
  const values = formats.float_repr.map((c: { value: number }, i: number) => (i === 1 ? -0 : c.value));

  formats.float_repr.forEach((c: { repr: string }, i: number) => {
    it(`repr(${c.repr})`, () => expect(pyFloatRepr(values[i])).toBe(c.repr));
  });

  formats.json_dumps.forEach((c: { text: string; dumps: string }) => {
    it(`json.dumps(json.loads(${c.text.slice(0, 30)}))`, () => {
      expect(pyJsonDumps(parseJsonPreservingNumbers(c.text))).toBe(c.dumps);
    });
  });

  formats.datetimes.forEach((c: { text: string; ms?: number; aware?: boolean; error?: string }) => {
    it(`pydantic datetime ${c.text}`, () => {
      const v = new Validator();
      const parsed = coerceDatetime(c.text, ["x"], v);
      if (c.error) {
        expect(parsed).toBe(INVALID);
        expect(v.errors[0].msg).toBe(`Input should be a valid datetime or date, ${c.error.replace(/^Input should be a valid datetime or date, /, "")}`);
      } else {
        expect(parsed).toEqual({ ms: c.ms, aware: c.aware });
      }
    });
  });
});

describe("Python string semantics", () => {
  it("strips Python whitespace, not JavaScript whitespace", () => {
    expect(pyStrip("\x1c\x1d text \x1e\x85")).toBe("text");
    const bom = String.fromCharCode(0xfeff);
    expect(pyStrip(`${bom}text${bom}`)).toBe(`${bom}text${bom}`);
  });

  it("counts code points like len()", () => {
    expect(pyLen("a🚀b")).toBe(3);
  });

  it("normalizes UUIDs like uuid.UUID()", () => {
    const id = "0f8fad5b-d9cb-469f-a165-70867728950e";
    expect(parseUuid(id.toUpperCase())).toBe(id);
    expect(parseUuid(`{${id}}`)).toBe(id);
    expect(parseUuid(`urn:uuid:${id}`)).toBe(id);
    expect(parseUuid(id.replace(/-/g, ""))).toBe(id);
    expect(parseUuid("nope")).toBeNull();
  });
});
