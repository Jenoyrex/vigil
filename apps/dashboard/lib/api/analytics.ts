import "server-only";

import { vigilFetch } from "./vigilClient";
import type { LlmUsageParams, LlmUsageResponse, SpanAnalyticsParams, SpanAnalyticsResponse } from "./types";

/** GET /v1/analytics/spans */
export function getSpanAnalytics(params: SpanAnalyticsParams): Promise<SpanAnalyticsResponse> {
  return vigilFetch<SpanAnalyticsResponse>("/v1/analytics/spans", params);
}

/** GET /v1/analytics/llm-usage */
export function getLlmUsageAnalytics(params: LlmUsageParams): Promise<LlmUsageResponse> {
  return vigilFetch<LlmUsageResponse>("/v1/analytics/llm-usage", params);
}
