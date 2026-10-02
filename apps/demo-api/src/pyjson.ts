// Python-compatible JSON parsing and formatting.
//
// apps/api stores several values as *text produced by Python*: a non-string
// span `input`/`output` is `json.dumps(value, ensure_ascii=False,
// separators=(",", ":"))`, and a numeric attribute value is `str(int)` /
// `str(float)`. JavaScript numbers cannot reproduce that (JSON `1.0` and `1`
// both parse to `1`; integers above 2^53 lose digits), so request bodies are
// parsed with the JSON source text of every number preserved.

/** A JSON number with its original source text (Python int vs float). */
export class JsonNumber {
  constructor(
    readonly source: string,
    readonly value: number,
  ) {}

  /** Python's json.loads yields an int for a literal with no `.`, `e` or `E`. */
  get isInt(): boolean {
    return !/[.eE]/.test(this.source);
  }
}

type ReviverContext = { source?: string };

/** JSON.parse that turns every number into a JsonNumber (throws SyntaxError). */
export function parseJsonPreservingNumbers(text: string): unknown {
  const reviver = (_key: string, value: unknown, context?: ReviverContext) =>
    typeof value === "number" ? new JsonNumber(context?.source ?? String(value), value) : value;
  return JSON.parse(text, reviver as (key: string, value: unknown) => unknown);
}

/** Python `repr(float)` / `str(float)`. */
export function pyFloatRepr(x: number): string {
  if (Number.isNaN(x)) return "nan";
  if (!Number.isFinite(x)) return x > 0 ? "inf" : "-inf";
  if (x === 0) return Object.is(x, -0) ? "-0.0" : "0.0";
  const sign = x < 0 ? "-" : "";
  // toExponential() with no argument yields the shortest round-trip digits.
  const [mantissa, expText] = Math.abs(x).toExponential().split("e");
  const digits = mantissa.replace(".", "");
  const exp = Number(expText);
  if (exp < -4 || exp >= 16) {
    const frac = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    const expAbs = String(Math.abs(exp)).padStart(2, "0");
    return `${sign}${frac}e${exp < 0 ? "-" : "+"}${expAbs}`;
  }
  if (exp < 0) return `${sign}0.${"0".repeat(-exp - 1)}${digits}`;
  const intPart = digits.slice(0, exp + 1).padEnd(exp + 1, "0");
  const fracPart = digits.slice(exp + 1) || "0";
  return `${sign}${intPart}.${fracPart}`;
}

/** Python `str()` of the int or float that json.loads produced for this number. */
export function pyNumberStr(n: JsonNumber): string {
  if (n.isInt) return BigInt(n.source).toString();
  return pyFloatRepr(n.value);
}

/** Python `json.dumps(value, ensure_ascii=False, separators=(",", ":"))`. */
export function pyJsonDumps(value: unknown): string {
  if (value instanceof JsonNumber) return pyNumberStr(value);
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : pyFloatRepr(value);
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(pyJsonDumps).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>);
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${pyJsonDumps(v)}`).join(",")}}`;
}

// Python str.isspace() / unicode `\s` (differs from JS: includes U+001C..U+001F
// and U+0085, excludes U+FEFF).
export const PY_WHITESPACE = String.fromCodePoint(
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
);
const PY_WS_CLASS = `[${PY_WHITESPACE}]`;
const PY_STRIP_RE = new RegExp(`^${PY_WS_CLASS}+|${PY_WS_CLASS}+$`, "gu");

/** Python `str.strip()`. */
export function pyStrip(s: string): string {
  return s.replace(PY_STRIP_RE, "");
}

/** Python `len(str)` (code points, not UTF-16 units). */
export function pyLen(s: string): number {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

const encoder = new TextEncoder();

export function utf8Length(s: string | null): number {
  return s ? encoder.encode(s).length : 0;
}
