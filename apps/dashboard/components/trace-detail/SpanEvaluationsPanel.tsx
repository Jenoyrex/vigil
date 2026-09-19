"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/Badge";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Skeleton } from "@/components/ui/Skeleton";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import {
  VigilApiError,
  type EvaluationResultOut,
  type SpanEvaluationsResponse,
} from "@/lib/api/types";
import { formatCost } from "@/lib/format";

interface FetchError {
  status: number;
  message: string;
}

function toFetchError(error: unknown): FetchError {
  if (error instanceof VigilApiError) return { status: error.status, message: error.message };
  return { status: 0, message: "Unable to load evaluation results." };
}

/**
 * Fetches `GET /v1/traces/{trace_id}/spans/{span_id}/evaluations` for the
 * currently-selected span only -- never once per span in the Waterfall (no
 * trace-scoped "all evaluations" endpoint exists to batch this against, and
 * none is added here). Mirrors TraceDetailView's existing deep-link
 * fallback-span fetch shape (a client-side `fetchVigilProxy` effect keyed
 * on the selected span), with an added cancellation guard so a fast span
 * re-selection can never let a slower, now-stale response overwrite a
 * newer one.
 */
export function SpanEvaluationsPanel({ traceId, spanId }: { traceId: string; spanId: string }) {
  const [status, setStatus] = useState<"loading" | "idle" | "error">("loading");
  const [results, setResults] = useState<EvaluationResultOut[]>([]);
  const [error, setError] = useState<FetchError | null>(null);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStatus("loading");
    setError(null);

    fetchVigilProxy<SpanEvaluationsResponse>(
      `/api/vigil/traces/${encodeURIComponent(traceId)}/spans/${encodeURIComponent(spanId)}/evaluations`,
    )
      .then((response) => {
        if (cancelled) return;
        setResults(response.results);
        setStatus("idle");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setStatus("error");
        setError(toFetchError(err));
      });

    return () => {
      cancelled = true;
    };
  }, [traceId, spanId]);

  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <p className="text-xs font-medium uppercase tracking-wide text-muted">Evaluations</p>

      {status === "loading" ? <Skeleton className="h-16 w-full" /> : null}

      {status === "error" && error ? <ErrorBanner message={error.message} /> : null}

      {status === "idle" && results.length === 0 ? (
        <p className="text-sm text-muted">No evaluations for this span yet.</p>
      ) : null}

      {status === "idle" && results.length > 0
        ? results.map((result) => (
            <div
              key={result.evaluation_id}
              className="space-y-1 rounded-md border border-border bg-surface p-3 text-sm"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs text-muted">
                  {result.evaluator_name}{" "}
                  <span className="text-foreground">{result.evaluator_version}</span>
                </span>
                <Badge tone="neutral">{result.label}</Badge>
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                <div>
                  <dt className="text-muted">Score</dt>
                  <dd className="font-mono text-foreground">{result.score ?? "—"}</dd>
                </div>
                <div>
                  <dt className="text-muted">Cost</dt>
                  <dd className="font-mono text-foreground">{formatCost(result.evaluation_cost_usd)}</dd>
                </div>
                {result.evaluator_model ? (
                  <div>
                    <dt className="text-muted">Model</dt>
                    <dd className="text-foreground">{result.evaluator_model}</dd>
                  </div>
                ) : null}
                {result.evaluator_provider ? (
                  <div>
                    <dt className="text-muted">Provider</dt>
                    <dd className="text-foreground">{result.evaluator_provider}</dd>
                  </div>
                ) : null}
              </dl>
              <p className="text-foreground">{result.explanation}</p>
            </div>
          ))
        : null}
    </div>
  );
}
