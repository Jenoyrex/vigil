import "server-only";

import { vigilFetch } from "./vigilClient";
import type {
  EvaluationJobListParams,
  EvaluationJobListResponse,
  EvaluatorConfigListResponse,
  EvaluatorConfigOut,
  EvaluatorConfigUpsertRequest,
  SpanEvaluationsResponse,
} from "./types";

/** GET /v1/evaluations/configs */
export function listEvaluatorConfigs(): Promise<EvaluatorConfigListResponse> {
  return vigilFetch<EvaluatorConfigListResponse>("/v1/evaluations/configs");
}

/** GET /v1/evaluations/configs/{evaluator_name} */
export function getEvaluatorConfig(evaluatorName: string): Promise<EvaluatorConfigOut> {
  return vigilFetch<EvaluatorConfigOut>(
    `/v1/evaluations/configs/${encodeURIComponent(evaluatorName)}`,
  );
}

/**
 * PUT /v1/evaluations/configs/{evaluator_name}
 *
 * The one write path in this app -- see lib/api/vigilClient.ts's
 * `VigilFetchInit`. `body` must always be the complete config (the API's
 * PUT is full-replace, not a partial patch); this function does not fill
 * in defaults itself, that's `EvaluatorConfigForm`'s job at the point the
 * user submits.
 */
export function upsertEvaluatorConfig(
  evaluatorName: string,
  body: EvaluatorConfigUpsertRequest,
): Promise<EvaluatorConfigOut> {
  return vigilFetch<EvaluatorConfigOut>(
    `/v1/evaluations/configs/${encodeURIComponent(evaluatorName)}`,
    undefined,
    { method: "PUT", body },
  );
}

/** GET /v1/evaluations/jobs */
export function listEvaluationJobs(params: EvaluationJobListParams): Promise<EvaluationJobListResponse> {
  return vigilFetch<EvaluationJobListResponse>("/v1/evaluations/jobs", params);
}

/**
 * GET /v1/traces/{trace_id}/spans/{span_id}/evaluations
 *
 * Span-scoped only -- there is no trace-scoped "all evaluations for this
 * trace" endpoint, so this must never be called once per span in a
 * Waterfall; it is only ever called for the single currently-selected span
 * (see components/trace-detail/SpanEvaluationsPanel.tsx).
 */
export function getSpanEvaluations(traceId: string, spanId: string): Promise<SpanEvaluationsResponse> {
  return vigilFetch<SpanEvaluationsResponse>(
    `/v1/traces/${encodeURIComponent(traceId)}/spans/${encodeURIComponent(spanId)}/evaluations`,
  );
}
