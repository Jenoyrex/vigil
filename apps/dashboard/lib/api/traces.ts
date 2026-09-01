import "server-only";

import { vigilFetch } from "./vigilClient";
import type {
  SpanDetailParams,
  SpanOut,
  TraceDetailParams,
  TraceDetailResponse,
  TraceListParams,
  TraceListResponse,
} from "./types";

/** GET /v1/traces */
export function listTraces(params: TraceListParams): Promise<TraceListResponse> {
  return vigilFetch<TraceListResponse>("/v1/traces", params);
}

/** GET /v1/traces/{trace_id} */
export function getTrace(
  traceId: string,
  params?: TraceDetailParams,
): Promise<TraceDetailResponse> {
  return vigilFetch<TraceDetailResponse>(
    `/v1/traces/${encodeURIComponent(traceId)}`,
    params,
  );
}

/**
 * GET /v1/traces/{trace_id}/spans/{span_id}
 *
 * Not used as a primary data path -- GET /v1/traces/{trace_id} already
 * inlines full span content for every loaded span, so selecting a span in
 * the waterfall reads already-fetched data with no network call. This is
 * only for the deep-link fallback: a `?span=` id not present in the
 * (possibly truncated) trace response.
 */
export function getSpan(
  traceId: string,
  spanId: string,
  params?: SpanDetailParams,
): Promise<SpanOut> {
  return vigilFetch<SpanOut>(
    `/v1/traces/${encodeURIComponent(traceId)}/spans/${encodeURIComponent(spanId)}`,
    params,
  );
}
