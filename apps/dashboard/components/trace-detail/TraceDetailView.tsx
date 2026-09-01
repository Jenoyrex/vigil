"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import type { SpanOut, TraceDetailResponse } from "@/lib/api/types";
import { buildSpanTree, findSpanNode } from "@/lib/tree";

import { SpanDetailPanel } from "./SpanDetailPanel";
import { TraceHeader } from "./TraceHeader";
import { Waterfall } from "./Waterfall";

export function TraceDetailView({ trace }: { trace: TraceDetailResponse }) {
  const searchParams = useSearchParams();
  const deepLinkedSpanId = searchParams.get("span");

  const roots = useMemo(() => buildSpanTree(trace.spans), [trace.spans]);
  const rootEnvironment = roots[0]?.span.environment;

  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(
    deepLinkedSpanId ?? roots[0]?.span.span_id ?? null,
  );

  // Deep-link fallback: GET /v1/traces/{trace_id}/spans/{span_id} is only
  // ever used here, for a span_id referenced by the URL that isn't present
  // in the (possibly truncated) trace response already loaded above.
  const [fallbackSpan, setFallbackSpan] = useState<SpanOut | null>(null);
  const [fallbackError, setFallbackError] = useState<string | null>(null);

  useEffect(() => {
    // Resetting fallback state here has no alternative trigger point -- it
    // must run whenever the deep-linked span id (or the loaded trace)
    // changes, and there is no user event to attach it to instead.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFallbackSpan(null);
    setFallbackError(null);
    if (!deepLinkedSpanId) return;
    if (findSpanNode(roots, deepLinkedSpanId)) return; // already loaded, no fetch needed

    fetchVigilProxy<SpanOut>(`/api/vigil/traces/${trace.trace_id}/spans/${deepLinkedSpanId}`)
      .then((span) => setFallbackSpan(span))
      .catch(() => setFallbackError("This span could not be found."));
  }, [deepLinkedSpanId, roots, trace.trace_id]);

  const selectedNode = selectedSpanId ? findSpanNode(roots, selectedSpanId) : null;
  const selectedSpan = fallbackSpan ?? selectedNode?.span ?? null;

  const traceStartMs = new Date(trace.start_time).getTime();

  return (
    <div className="space-y-4">
      <TraceHeader trace={trace} environment={rootEnvironment} />

      {roots.length === 0 ? (
        <p className="text-sm text-muted">This trace has no loaded spans.</p>
      ) : (
        <div className="flex flex-col gap-4 lg:flex-row">
          <div className="min-w-0 lg:flex-[3]">
            <Waterfall
              roots={roots}
              traceStartMs={traceStartMs}
              traceDurationMs={trace.duration_ms}
              selectedSpanId={selectedSpan?.span_id ?? null}
              onSelect={setSelectedSpanId}
            />
          </div>
          <div className="min-w-0 lg:flex-[2]">
            {fallbackError ? (
              <ErrorBanner message={fallbackError} />
            ) : selectedSpan ? (
              <SpanDetailPanel span={selectedSpan} />
            ) : (
              <p className="text-sm text-muted">Select a span to view its details.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
