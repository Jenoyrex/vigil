// POST /v1/traces request validation and row transformation: a port of
// apps/api/app/schemas/traces.py (TracesRequest/SpanIn/EventIn/ResourceModel)
// and apps/api/app/services/ingestion.py (transform_request). Pinned by
// test/fixtures/ingest-parity.json, generated from those production modules.

import { MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MAX_SPANS_PER_REQUEST, MAX_TOTAL_SPAN_BYTES } from "./config";
import { type Loc, validationError } from "./http";
import { JsonNumber, pyFloatRepr, pyJsonDumps, pyNumberStr, pyStrip } from "./pyjson";
import {
  INVALID,
  type Maybe,
  type Obj,
  type ParsedTime,
  Validator,
  coerceDatetime,
  coerceFloat,
  isObject,
  optionalNonNegativeInt,
  optionalStr,
  requiredStr,
} from "./validation";

/** One span as stored (the ClickHouse `spans` row). */
export interface SpanRow {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  span_type: string;
  resource: string;
  start_time: number;
  end_time: number;
  duration_ms: number;
  status: "unset" | "ok" | "error";
  status_message: string | null;
  input: string | null;
  input_size_bytes: number;
  input_truncated: boolean;
  output: string | null;
  output_size_bytes: number;
  output_truncated: boolean;
  attributes: Map<string, string>;
  attributes_truncated: boolean;
  events: { time: number; name: string; attributes: Map<string, string> }[];
  events_truncated: boolean;
  llm_provider: string | null;
  llm_model: string | null;
  llm_input_tokens: number | null;
  llm_output_tokens: number | null;
  llm_total_tokens: number | null;
  llm_cost_micros: number | null;
  environment: string;
  release: string | null;
}

// ---- validation ------------------------------------------------------------

type AttrValue = string | boolean | JsonNumber;

const UINT32_MAX = 4294967295;
const DECIMAL64_6_MAX = 9223372036854.775807;

function attributes(value: unknown, loc: Loc, v: Validator): Maybe<[string, AttrValue][] | null> {
  if (value === undefined || value === null) return null;
  if (!isObject(value)) return v.add("dict_type", loc, "Input should be a valid dictionary");
  const out: [string, AttrValue][] = [];
  let ok = true;
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "string" || typeof item === "boolean" || item instanceof JsonNumber) {
      out.push([key, item]);
      continue;
    }
    // Pydantic's smart union reports one error per member type.
    v.add("string_type", [...loc, key, "str"], "Input should be a valid string");
    v.add("int_type", [...loc, key, "int"], "Input should be a valid integer");
    v.add("float_type", [...loc, key, "float"], "Input should be a valid number");
    v.add("bool_type", [...loc, key, "bool"], "Input should be a valid boolean");
    ok = false;
  }
  return ok ? out : INVALID;
}

function datetimeField(obj: Obj, key: string, loc: Loc, v: Validator): Maybe<ParsedTime> {
  if (!(key in obj)) return v.add("missing", [...loc, key], "Field required");
  return coerceDatetime(obj[key], [...loc, key], v);
}

interface EventIn {
  time: ParsedTime;
  name: string;
  attributes: [string, AttrValue][] | null;
}

function events(value: unknown, loc: Loc, v: Validator): Maybe<EventIn[] | null> {
  if (value === undefined || value === null) return null;
  if (!Array.isArray(value)) return v.add("list_type", loc, "Input should be a valid list");
  const out: EventIn[] = [];
  let ok = true;
  value.forEach((item, i) => {
    const itemLoc = [...loc, i];
    if (!isObject(item)) {
      v.add("model_type", itemLoc, "Input should be a valid dictionary or instance of EventIn");
      ok = false;
      return;
    }
    const time = datetimeField(item, "time", itemLoc, v);
    const name = requiredStr(item, "name", itemLoc, v, { minLength: 1 });
    const attrs = attributes(item.attributes, [...itemLoc, "attributes"], v);
    if (time === INVALID || name === INVALID || attrs === INVALID) ok = false;
    else out.push({ time, name, attributes: attrs });
  });
  return ok ? out : INVALID;
}

function nonNegativeFloat(obj: Obj, key: string, loc: Loc, v: Validator): Maybe<number | null> {
  const value = obj[key];
  if (value === undefined || value === null) return null;
  const n = coerceFloat(value, [...loc, key], v);
  if (n === INVALID) return INVALID;
  if (!(n >= 0)) return v.add("greater_than_equal", [...loc, key], "Input should be greater than or equal to 0");
  if (!Number.isFinite(n)) return v.add("finite_number", [...loc, key], "Input should be a finite number");
  return n;
}

const STATUSES = ["unset", "ok", "error"] as const;

interface SpanIn {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  span_type: string;
  start: ParsedTime;
  end: ParsedTime;
  status: (typeof STATUSES)[number];
  status_message: string | null;
  input: unknown;
  output: unknown;
  attributes: [string, AttrValue][] | null;
  events: EventIn[] | null;
  llm_provider: string | null;
  llm_model: string | null;
  llm_input_tokens: number | null;
  llm_output_tokens: number | null;
  llm_total_tokens: number | null;
  llm_cost_usd: number | null;
  environment: string | null;
  release: string | null;
}

function span(item: unknown, loc: Loc, v: Validator): Maybe<SpanIn> {
  if (!isObject(item)) return v.add("model_type", loc, "Input should be a valid dictionary or instance of SpanIn");
  const before = v.errors.length;
  const trace_id = requiredStr(item, "trace_id", loc, v);
  const span_id = requiredStr(item, "span_id", loc, v);
  const parent_span_id = optionalStr(item, "parent_span_id", loc, v);
  const name = requiredStr(item, "name", loc, v, { minLength: 1 });
  const span_type = "span_type" in item ? requiredStr(item, "span_type", loc, v, { minLength: 1 }) : "unknown";
  const start = datetimeField(item, "start_time", loc, v);
  const end = datetimeField(item, "end_time", loc, v);
  let status: Maybe<SpanIn["status"]> = "unset";
  if ("status" in item) {
    status = (STATUSES as readonly unknown[]).includes(item.status)
      ? (item.status as SpanIn["status"])
      : v.add("literal_error", [...loc, "status"], "Input should be 'unset', 'ok' or 'error'");
  }
  const status_message = optionalStr(item, "status_message", loc, v);
  const attrs = attributes(item.attributes, [...loc, "attributes"], v);
  const evs = events(item.events, [...loc, "events"], v);
  const llm_provider = optionalStr(item, "llm_provider", loc, v);
  const llm_model = optionalStr(item, "llm_model", loc, v);
  const llm_input_tokens = optionalNonNegativeInt(item, "llm_input_tokens", loc, v);
  const llm_output_tokens = optionalNonNegativeInt(item, "llm_output_tokens", loc, v);
  const llm_total_tokens = optionalNonNegativeInt(item, "llm_total_tokens", loc, v);
  const llm_cost_usd = nonNegativeFloat(item, "llm_cost_usd", loc, v);
  const environment = optionalStr(item, "environment", loc, v);
  const release = optionalStr(item, "release", loc, v);
  if (v.errors.length > before) return INVALID;

  // Storage-range checks (ClickHouse UInt32 / Decimal64(6) would reject these).
  for (const [key, n] of [["llm_input_tokens", llm_input_tokens], ["llm_output_tokens", llm_output_tokens], ["llm_total_tokens", llm_total_tokens]] as const) {
    if (typeof n === "number" && n > UINT32_MAX) {
      return v.add("less_than_equal", [...loc, key], `Input should be less than or equal to ${UINT32_MAX}`);
    }
  }
  if (typeof llm_cost_usd === "number" && llm_cost_usd > DECIMAL64_6_MAX) {
    return v.add("less_than_equal", [...loc, "llm_cost_usd"], `Input should be less than or equal to ${DECIMAL64_6_MAX}`);
  }

  // SpanIn._validate_ids_and_times (model_validator mode="after").
  const valueError = (msg: string): typeof INVALID => v.add("value_error", loc, `Value error, ${msg}`);
  if (!/^[0-9a-fA-F]{32}$/.test(trace_id as string)) return valueError("trace_id must be exactly 32 hexadecimal characters");
  if (!/^[0-9a-fA-F]{16}$/.test(span_id as string)) return valueError("span_id must be exactly 16 hexadecimal characters");
  if (parent_span_id !== null && !/^[0-9a-fA-F]{16}$/.test(parent_span_id as string)) {
    return valueError("span_id must be exactly 16 hexadecimal characters");
  }
  const strippedName = pyStrip(name as string);
  if (!strippedName) return valueError("name must not be empty");
  // Production compares naive and aware datetimes directly and fails with a
  // TypeError (HTTP 500) when they are mixed; storage treats naive as UTC, so
  // the demo compares in UTC instead.
  if ((end as ParsedTime).ms < (start as ParsedTime).ms) return valueError("end_time must be >= start_time");

  return {
    trace_id: (trace_id as string).toLowerCase(),
    span_id: (span_id as string).toLowerCase(),
    parent_span_id: parent_span_id === null ? null : (parent_span_id as string).toLowerCase(),
    name: strippedName,
    span_type: span_type as string,
    start: start as ParsedTime,
    end: end as ParsedTime,
    status: status as SpanIn["status"],
    status_message: status_message as string | null,
    input: item.input,
    output: item.output,
    attributes: attrs as SpanIn["attributes"],
    events: evs as SpanIn["events"],
    llm_provider: llm_provider as string | null,
    llm_model: llm_model as string | null,
    llm_input_tokens: llm_input_tokens as number | null,
    llm_output_tokens: llm_output_tokens as number | null,
    llm_total_tokens: llm_total_tokens as number | null,
    llm_cost_usd: llm_cost_usd as number | null,
    environment: environment as string | null,
    release: release as string | null,
  };
}

const RESOURCE_FIELDS: Record<string, "sdk_name" | "sdk_version" | "service_name"> = {
  "sdk.name": "sdk_name",
  "sdk.version": "sdk_version",
  "service.name": "service_name",
  sdk_name: "sdk_name",
  sdk_version: "sdk_version",
  service_name: "service_name",
};

interface Resource {
  sdk_name: string | null;
  sdk_version: string | null;
  service_name: string | null;
  extra: [string, unknown][];
}

function resource(body: Obj, v: Validator): Maybe<Resource> {
  const out: Resource = { sdk_name: null, sdk_version: null, service_name: null, extra: [] };
  if (!("resource" in body)) return out;
  const value = body.resource;
  if (!isObject(value)) return v.add("model_type", ["body", "resource"], "Input should be a valid dictionary or instance of ResourceModel");
  let ok = true;
  for (const [key, item] of Object.entries(value)) {
    const field = RESOURCE_FIELDS[key];
    if (!field) {
      out.extra.push([key, item]);
      continue;
    }
    if (item === null) continue;
    if (typeof item !== "string") {
      v.add("string_type", ["body", "resource", key], "Input should be a valid string");
      ok = false;
      continue;
    }
    // By alias ("service.name") wins over by name ("service_name").
    if (key.includes(".") || out[field] === null) out[field] = item;
  }
  return ok ? out : INVALID;
}

export function validateTracesRequest(body: Obj): { resource: Resource; spans: SpanIn[] } {
  const v = new Validator();
  const res = resource(body, v);
  let spans: SpanIn[] = [];
  if (!("spans" in body)) {
    v.add("missing", ["body", "spans"], "Field required");
  } else if (!Array.isArray(body.spans)) {
    v.add("list_type", ["body", "spans"], "Input should be a valid list");
  } else if (body.spans.length < 1) {
    v.add("too_short", ["body", "spans"], "List should have at least 1 item after validation, not 0");
  } else if (body.spans.length > MAX_SPANS_PER_REQUEST) {
    v.add("too_long", ["body", "spans"], `List should have at most ${MAX_SPANS_PER_REQUEST} items after validation, not ${body.spans.length}`);
  } else {
    spans = body.spans.map((s, i) => span(s, ["body", "spans", i], v)).filter((s): s is SpanIn => s !== INVALID);
  }
  if (v.errors.length > 0 || res === INVALID) throw validationError(v.errors);
  return { resource: res as Resource, spans };
}

// ---- transformation (services/ingestion.py) --------------------------------

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const byteLen = (s: string | null) => (s ? encoder.encode(s).length : 0);

function coerceAttrValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return value;
  if (value instanceof JsonNumber) return pyNumberStr(value);
  return pyJsonDumps(value);
}

function coerceAttributes(attrs: [string, unknown][] | null): Map<string, string> {
  return new Map((attrs ?? []).map(([k, value]) => [k, coerceAttrValue(value)]));
}

function normalizeText(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value === "string") return value;
  return pyJsonDumps(value);
}

/** `_truncate_field`: UTF-8 byte truncation, dropping a split trailing character. */
function truncateField(value: unknown, maxBytes: number): [string | null, number, boolean] {
  const text = normalizeText(value);
  if (text === null) return [null, 0, false];
  const encoded = encoder.encode(text);
  if (encoded.length <= maxBytes) return [text, encoded.length, false];
  let cut = maxBytes;
  while (cut > 0 && (encoded[cut] & 0xc0) === 0x80) cut--;
  return [decoder.decode(encoded.subarray(0, cut)), encoded.length, true];
}

function fitAttributes(attrs: Map<string, string>, budget: number): [Map<string, string>, boolean, number] {
  const kept = new Map<string, string>();
  let used = 0;
  for (const [key, value] of attrs) {
    const size = byteLen(key) + byteLen(value);
    if (used + size > budget) return [kept, true, budget - used];
    kept.set(key, value);
    used += size;
  }
  return [kept, false, budget - used];
}

function fitEvents(events: SpanRow["events"], budget: number): [SpanRow["events"], boolean] {
  const kept: SpanRow["events"] = [];
  let used = 0;
  for (const event of events) {
    let size = byteLen(event.name) + 8; // +8: rough fixed cost of the timestamp
    for (const [k, value] of event.attributes) size += byteLen(k) + byteLen(value);
    if (used + size > budget) return [kept, true];
    kept.push(event);
    used += size;
  }
  return [kept, false];
}

/**
 * Decimal64(6) storage: clickhouse_connect writes int(Decimal(str(x)) * 10**6),
 * i.e. the Python float repr truncated toward zero at 6 decimal places.
 */
export function costMicros(value: number): number {
  const repr = pyFloatRepr(value);
  const m = /^(\d+)(?:\.(\d+))?(?:e([+-]\d+))?$/.exec(repr);
  if (!m) throw new Error(`unexpected float repr ${repr}`);
  const digits = m[1] + (m[2] ?? "");
  const scale = (m[2]?.length ?? 0) - Number(m[3] ?? 0) - 6; // digits / 10**scale
  const big = BigInt(digits);
  return Number(scale <= 0 ? big * 10n ** BigInt(-scale) : big / 10n ** BigInt(scale));
}

export function transformRequest(request: { resource: Resource; spans: SpanIn[] }): SpanRow[] {
  const { resource: res } = request;
  const resourceAttributes = new Map<string, string>();
  if (res.sdk_name) resourceAttributes.set("resource.sdk.name", res.sdk_name);
  if (res.sdk_version) resourceAttributes.set("resource.sdk.version", res.sdk_version);
  for (const [key, value] of res.extra) resourceAttributes.set(`resource.${key}`, coerceAttrValue(value));

  return request.spans.map((s) => {
    const [input, inputSize, inputTruncated] = truncateField(s.input, MAX_INPUT_BYTES);
    const [output, outputSize, outputTruncated] = truncateField(s.output, MAX_OUTPUT_BYTES);
    const merged = new Map([...resourceAttributes, ...coerceAttributes(s.attributes)]);
    const eventRows = (s.events ?? []).map((e) => ({ time: e.time.ms, name: e.name, attributes: coerceAttributes(e.attributes) }));
    const remaining = Math.max(0, MAX_TOTAL_SPAN_BYTES - byteLen(input) - byteLen(output));
    const [keptAttributes, attributesTruncated, afterAttributes] = fitAttributes(merged, remaining);
    const [keptEvents, eventsTruncated] = fitEvents(eventRows, afterAttributes);
    return {
      trace_id: s.trace_id,
      span_id: s.span_id,
      parent_span_id: s.parent_span_id,
      name: s.name,
      span_type: s.span_type,
      resource: res.service_name || "",
      start_time: s.start.ms,
      end_time: s.end.ms,
      duration_ms: s.end.ms - s.start.ms,
      status: s.status,
      status_message: s.status_message,
      input,
      input_size_bytes: inputSize,
      input_truncated: inputTruncated,
      output,
      output_size_bytes: outputSize,
      output_truncated: outputTruncated,
      attributes: keptAttributes,
      attributes_truncated: attributesTruncated,
      events: keptEvents,
      events_truncated: eventsTruncated,
      llm_provider: s.llm_provider,
      llm_model: s.llm_model,
      llm_input_tokens: s.llm_input_tokens,
      llm_output_tokens: s.llm_output_tokens,
      llm_total_tokens: s.llm_total_tokens,
      llm_cost_micros: s.llm_cost_usd === null ? null : costMicros(s.llm_cost_usd),
      environment: s.environment || "unknown",
      release: s.release,
    };
  });
}
