/**
 * TypeScript mirror of the Vigil API's read-side response schemas
 * (apps/api/app/schemas/query.py, apps/api/app/schemas/analytics.py).
 *
 * Field names and shapes are kept identical to the Pydantic models on
 * purpose -- this file has no business logic of its own, only the shape of
 * what the API already returns. If a field is added/renamed on the backend,
 * this file must be updated to match; it must never diverge and compensate
 * for a mismatch with client-side logic.
 *
 * `total_cost_usd` and `llm_cost_usd` are `string`, not `number` -- the API
 * serializes ClickHouse `Decimal64(6)` values as strings specifically to
 * avoid JS floating-point precision loss (see ADR 003 section 2). Nothing
 * in this file, or anywhere in lib/api/, parses them to a number. Only
 * lib/format.ts is allowed to do that, at the point of display.
 */

// -- Trace list (GET /v1/traces) --------------------------------------------

export type TraceStatus = "ok" | "error" | "unknown";
export type SpanStatus = "unset" | "ok" | "error";

export interface TraceSummary {
  trace_id: string;
  start_time: string;
  end_time: string;
  duration_ms: number;
  status: TraceStatus;
  span_count: number;
  error_span_count: number;
  root_span_name: string | null;
  environment: string;
  resource: string;
}

export interface TraceListResponse {
  traces: TraceSummary[];
  next_cursor: string | null;
}

export interface TraceListParams {
  [key: string]: string | number | boolean | undefined;
  start_time_from?: string;
  start_time_to?: string;
  environment?: string;
  resource?: string;
  has_error?: boolean;
  limit?: number;
  cursor?: string;
}

// -- Trace detail / span detail ---------------------------------------------

export interface SpanEvent {
  time: string;
  name: string;
  attributes: Record<string, string>;
}

export interface SpanOut {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  span_type: string;
  resource: string;
  start_time: string;
  end_time: string;
  duration_ms: number;
  status: SpanStatus;
  status_message: string | null;
  input: string | null;
  input_size_bytes: number;
  input_truncated: boolean;
  output: string | null;
  output_size_bytes: number;
  output_truncated: boolean;
  attributes: Record<string, string>;
  attributes_truncated: boolean;
  events: SpanEvent[];
  events_truncated: boolean;
  llm_provider: string | null;
  llm_model: string | null;
  llm_input_tokens: number | null;
  llm_output_tokens: number | null;
  llm_total_tokens: number | null;
  /** Decimal string, e.g. "0.000340" -- never parse to a number here. */
  llm_cost_usd: string | null;
  environment: string;
  release: string | null;
}

export interface TraceDetailResponse {
  trace_id: string;
  status: TraceStatus;
  start_time: string;
  end_time: string;
  duration_ms: number;
  span_count: number;
  total_span_count: number;
  truncated: boolean;
  spans: SpanOut[];
}

export interface TraceDetailParams {
  [key: string]: string | number | boolean | undefined;
  /** Optional YYYY-MM-DD partition-pruning hint. */
  start_date?: string;
}

export type SpanDetailParams = TraceDetailParams;

// -- Analytics: spans (GET /v1/analytics/spans) ------------------------------

export type SpanGroupBy = "environment" | "span_type" | "release" | "resource";
export type SpanBucket = "hour" | "day";

export interface LatencyPercentiles {
  p50: number;
  p90: number;
  p99: number;
}

export interface SpanAnalyticsGroup {
  value: string;
  span_count: number;
  error_span_count: number;
  error_rate: number;
  latency_ms: LatencyPercentiles;
}

export interface SpanAnalyticsBucket {
  bucket_start: string;
  span_count: number;
  error_span_count: number;
  error_rate: number;
  latency_ms: LatencyPercentiles;
}

/**
 * Exactly one of the flat fields / `groups` / `buckets` is populated,
 * matching whichever of `group_by`/`bucket` (mutually exclusive) the
 * request used.
 */
export interface SpanAnalyticsResponse {
  start_time_from: string;
  start_time_to: string;
  group_by: SpanGroupBy | null;
  bucket: SpanBucket | null;
  span_count: number | null;
  error_span_count: number | null;
  error_rate: number | null;
  latency_ms: LatencyPercentiles | null;
  groups: SpanAnalyticsGroup[] | null;
  buckets: SpanAnalyticsBucket[] | null;
}

export interface SpanAnalyticsParams {
  [key: string]: string | number | boolean | undefined;
  start_time_from?: string;
  start_time_to?: string;
  environment?: string;
  resource?: string;
  span_type?: string;
  group_by?: SpanGroupBy;
  bucket?: SpanBucket;
}

// -- Analytics: LLM usage (GET /v1/analytics/llm-usage) ----------------------

export type LlmGroupBy = "llm_provider" | "llm_model" | "environment";

export interface LlmUsageGroup {
  value: string;
  llm_span_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  /** Decimal string -- never parse to a number here. */
  total_cost_usd: string;
}

export interface LlmUsageResponse {
  start_time_from: string;
  start_time_to: string;
  group_by: LlmGroupBy | null;
  llm_span_count: number | null;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  total_tokens: number | null;
  /** Decimal string -- never parse to a number here. */
  total_cost_usd: string | null;
  groups: LlmUsageGroup[] | null;
}

export interface LlmUsageParams {
  [key: string]: string | number | boolean | undefined;
  start_time_from?: string;
  start_time_to?: string;
  environment?: string;
  group_by?: LlmGroupBy;
}

// -- Error shape --------------------------------------------------------

/** One item of FastAPI's automatic Pydantic validation-error format. */
export interface ApiValidationErrorItem {
  msg?: string;
  loc?: (string | number)[];
}

/**
 * The Vigil API's error body has two different shapes for the same
 * `detail` key, depending on how the 422/etc. was produced:
 * - `HTTPException(status_code=..., detail="some string")` (explicit,
 *   e.g. app/services/query.py's QueryValidationError) -> `detail` is a
 *   plain string.
 * - FastAPI's own automatic request validation (e.g. a malformed path
 *   parameter failing a Pydantic `AfterValidator`, before any route code
 *   runs) -> `detail` is an *array* of structured error objects.
 * Both are real, both come from the same API, and code that only handles
 * the string case renders "[object Object]" for the second -- see
 * `extractDetailMessage` below, which every error-parsing path must go
 * through instead of reading `body.detail` directly.
 */
export interface ApiErrorBody {
  detail?: string | ApiValidationErrorItem[];
}

/** Normalizes either `ApiErrorBody.detail` shape into one display string. */
export function extractDetailMessage(detail: ApiErrorBody["detail"]): string | undefined {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item && typeof item.msg === "string" ? item.msg : null))
      .filter((msg): msg is string => msg !== null);
    return messages.length > 0 ? messages.join("; ") : undefined;
  }
  return undefined;
}

/** Thrown by lib/api/vigilClient.ts and lib/api/browserClient.ts on a non-2xx response. */
export class VigilApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "VigilApiError";
    this.status = status;
  }
}
