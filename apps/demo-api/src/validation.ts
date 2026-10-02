// Pydantic v2 (lax mode) request validation, reproducing apps/api's error
// items (`type`, `loc`, `msg`). Behavior is pinned by
// test/fixtures/*-parity.json, generated from the production schemas.

import { type Loc, type ValidationItem, validationError } from "./http";
import { JsonNumber, pyLen, pyStrip } from "./pyjson";

export const INVALID = Symbol("invalid");
export type Maybe<T> = T | typeof INVALID;

export class Validator {
  readonly errors: ValidationItem[] = [];

  add(type: string, loc: Loc, msg: string): typeof INVALID {
    this.errors.push({ type, loc, msg });
    return INVALID;
  }

  throwIfInvalid(): void {
    if (this.errors.length > 0) throw validationError(this.errors);
  }
}

export type Obj = Record<string, unknown>;

export function isObject(value: unknown): value is Obj {
  return typeof value === "object" && value !== null && !Array.isArray(value) && !(value instanceof JsonNumber);
}

/** Parse a request body that must be a JSON object (FastAPI's body errors). */
export function requireObjectBody(parsed: unknown, v: Validator): Obj {
  if (!isObject(parsed)) {
    v.add("model_attributes_type", ["body"], "Input should be a valid dictionary or object to extract fields from");
    v.throwIfInvalid();
  }
  return parsed as Obj;
}

interface StrOptions {
  minLength?: number;
  maxLength?: number;
  stripWhitespace?: boolean;
}

function checkStr(value: unknown, loc: Loc, v: Validator, opts: StrOptions): Maybe<string> {
  if (typeof value !== "string") return v.add("string_type", loc, "Input should be a valid string");
  const s = opts.stripWhitespace ? pyStrip(value) : value;
  const length = pyLen(s);
  if (opts.minLength !== undefined && length < opts.minLength) {
    const unit = opts.minLength === 1 ? "character" : "characters";
    return v.add("string_too_short", loc, `String should have at least ${opts.minLength} ${unit}`);
  }
  if (opts.maxLength !== undefined && length > opts.maxLength) {
    const unit = opts.maxLength === 1 ? "character" : "characters";
    return v.add("string_too_long", loc, `String should have at most ${opts.maxLength} ${unit}`);
  }
  return s;
}

export function requiredStr(obj: Obj, key: string, loc: Loc, v: Validator, opts: StrOptions = {}): Maybe<string> {
  if (!(key in obj)) return v.add("missing", [...loc, key], "Field required");
  return checkStr(obj[key], [...loc, key], v, opts);
}

export function optionalStr(obj: Obj, key: string, loc: Loc, v: Validator, opts: StrOptions = {}): Maybe<string | null> {
  const value = obj[key];
  if (value === undefined || value === null) return null;
  return checkStr(value, [...loc, key], v, opts);
}

/** Pydantic lax `int` (accepts bools, whole floats, numeric strings). */
export function coerceInt(value: unknown, loc: Loc, v: Validator): Maybe<number> {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (value instanceof JsonNumber) {
    if (value.isInt) return value.value;
    if (Number.isInteger(value.value)) return value.value;
    return v.add("int_from_float", loc, "Input should be a valid integer, got a number with a fractional part");
  }
  if (typeof value === "string") {
    const m = /^\s*([+-]?\d+)(?:\.0*)?\s*$/.exec(value);
    if (m) return Number(m[1]);
    return v.add("int_parsing", loc, "Input should be a valid integer, unable to parse string as an integer");
  }
  return v.add("int_type", loc, "Input should be a valid integer");
}

/** Pydantic lax `float`. */
export function coerceFloat(value: unknown, loc: Loc, v: Validator): Maybe<number> {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (value instanceof JsonNumber) return value.value;
  if (typeof value === "string") {
    const t = value.trim();
    if (/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(t)) return Number(t);
    return v.add("float_parsing", loc, "Input should be a valid number, unable to parse string as a number");
  }
  return v.add("float_type", loc, "Input should be a valid number");
}

export function optionalNonNegativeInt(obj: Obj, key: string, loc: Loc, v: Validator): Maybe<number | null> {
  const value = obj[key];
  if (value === undefined || value === null) return null;
  const n = coerceInt(value, [...loc, key], v);
  if (n === INVALID) return INVALID;
  if (n < 0) return v.add("greater_than_equal", [...loc, key], "Input should be greater than or equal to 0");
  return n;
}

// ---- datetimes (pydantic's speedate parser, subset) ------------------------

export interface ParsedTime {
  ms: number;
  aware: boolean;
}

const DATE_PREFIX = "Input should be a valid datetime or date, ";

function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function unixToMs(value: number): number {
  // Pydantic treats magnitudes above 2e10 as milliseconds, below as seconds.
  return Math.floor(Math.abs(value) > 2e10 ? value : value * 1000);
}

type ParseResult = ParsedTime | { error: string };

function parseDatetimeString(s: string): ParseResult {
  if (/^\s*[+-]?\d+(\.\d+)?\s*$/.test(s)) return { ms: unixToMs(Number(s)), aware: true };
  if (s.length < 10) return { error: "input is too short" };
  if (!/^\d{4}$/.test(s.slice(0, 4))) return { error: "invalid character in year" };
  if (s[4] !== "-" || s[7] !== "-") return { error: "invalid date separator, expected `-`" };
  if (!/^\d{2}$/.test(s.slice(5, 7))) return { error: "invalid character in month" };
  if (!/^\d{2}$/.test(s.slice(8, 10))) return { error: "invalid character in day" };
  const year = Number(s.slice(0, 4));
  const month = Number(s.slice(5, 7));
  const day = Number(s.slice(8, 10));
  if (month < 1 || month > 12) return { error: "month value is outside expected range" };
  if (day < 1 || day > daysInMonth(year, month)) return { error: "day value is outside expected range" };
  const dateMs = Date.UTC(year, month - 1, day);
  if (s.length === 10) return { ms: dateMs, aware: false };

  const time = /^[Tt _](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?(?:([Zz])|([+-])(\d{2}):?(\d{2})?)?$/.exec(s.slice(10));
  if (!time) return { error: "unexpected extra characters at the end of the input" };
  const [, hh, mm, ss = "0", frac = "", zulu, sign, offH, offM = "0"] = time;
  if (Number(hh) > 23 || Number(mm) > 59 || Number(ss) > 59) {
    return { error: "unexpected extra characters at the end of the input" };
  }
  const millis = Number(frac.slice(0, 3).padEnd(3, "0"));
  let ms = dateMs + ((Number(hh) * 60 + Number(mm)) * 60 + Number(ss)) * 1000 + millis;
  if (sign) ms -= (sign === "-" ? -1 : 1) * (Number(offH) * 60 + Number(offM)) * 60000;
  return { ms, aware: Boolean(zulu || sign) };
}

/** Pydantic `datetime` field. Naive values are returned with `aware: false`. */
export function coerceDatetime(value: unknown, loc: Loc, v: Validator): Maybe<ParsedTime> {
  if (value instanceof JsonNumber) return { ms: unixToMs(value.value), aware: true };
  if (typeof value === "string") {
    const result = parseDatetimeString(value);
    if ("error" in result) return v.add("datetime_from_date_parsing", loc, DATE_PREFIX + result.error);
    return result;
  }
  return v.add("datetime_type", loc, "Input should be a valid datetime");
}

/** apps/api's `AwareDatetime` query parameter. */
export function queryAwareDatetime(raw: string | null, name: string, v: Validator): Maybe<number | null> {
  if (raw === null) return null;
  const parsed = coerceDatetime(raw, ["query", name], v);
  if (parsed === INVALID) return INVALID;
  if (!parsed.aware) {
    return v.add(
      "value_error",
      ["query", name],
      "Value error, must be a timezone-aware RFC3339 timestamp (include a UTC offset)",
    );
  }
  return parsed.ms;
}

export function queryLiteral<T extends string>(
  raw: string | null,
  name: string,
  allowed: readonly T[],
  v: Validator,
): Maybe<T | null> {
  if (raw === null) return null;
  if ((allowed as readonly string[]).includes(raw)) return raw as T;
  const quoted = allowed.map((a) => `'${a}'`);
  const list = quoted.length > 1 ? `${quoted.slice(0, -1).join(", ")} or ${quoted.at(-1)}` : quoted[0];
  return v.add("literal_error", ["query", name], `Input should be ${list}`);
}

export function queryBool(raw: string | null, name: string, v: Validator): Maybe<boolean | null> {
  if (raw === null) return null;
  const t = raw.trim().toLowerCase();
  if (["1", "on", "t", "true", "y", "yes"].includes(t)) return true;
  if (["0", "off", "f", "false", "n", "no"].includes(t)) return false;
  return v.add("bool_parsing", ["query", name], "Input should be a valid boolean, unable to interpret input");
}

export function queryLimit(raw: string | null, v: Validator, max = 100): Maybe<number> {
  if (raw === null) return 20;
  const n = coerceInt(raw, ["query", "limit"], v);
  if (n === INVALID) return INVALID;
  if (n < 1) return v.add("greater_than_equal", ["query", "limit"], "Input should be greater than or equal to 1");
  if (n > max) return v.add("less_than_equal", ["query", "limit"], `Input should be less than or equal to ${max}`);
  return n;
}

const UUID_RE = /^(?:urn:uuid:)?\{?([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})\}?$/i;

/** Python `uuid.UUID(str)` normalization; null if invalid. */
export function parseUuid(raw: string | null | undefined): string | null {
  const m = raw ? UUID_RE.exec(raw.trim()) : null;
  return m ? m.slice(1, 6).join("-").toLowerCase() : null;
}

export function pathUuid(raw: string, name: string, v: Validator): Maybe<string> {
  const id = parseUuid(raw);
  if (id !== null) return id;
  // Pydantic appends a parser-specific reason; the stable prefix is kept.
  return v.add("uuid_parsing", ["path", name], "Input should be a valid UUID");
}

export function pathHexId(raw: string, name: "trace_id" | "span_id", v: Validator): Maybe<string> {
  const length = name === "trace_id" ? 32 : 16;
  if (new RegExp(`^[0-9a-fA-F]{${length}}$`).test(raw)) return raw.toLowerCase();
  const words = name === "trace_id" ? "32" : "16";
  return v.add("value_error", ["path", name], `Value error, ${name} must be exactly ${words} hexadecimal characters`);
}

const EVALUATOR_NAME_RE = /^[A-Za-z0-9_.-]{1,128}$/;

export function checkEvaluatorName(raw: string, loc: Loc, v: Validator): Maybe<string> {
  if (EVALUATOR_NAME_RE.test(raw)) return raw;
  return v.add(
    "value_error",
    loc,
    "Value error, evaluator_name must be 1-128 characters, using only letters, digits, underscore, hyphen, or period.",
  );
}
