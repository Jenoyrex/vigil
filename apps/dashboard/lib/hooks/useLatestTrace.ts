"use client";

import { useEffect, useState } from "react";

import { fetchVigilProxy } from "@/lib/api/browserClient";
import type { TraceListResponse, TraceSummary } from "@/lib/api/types";

const FUTURE_SKEW_MS = 60_000; // tolerate a client clock slightly ahead of ours
// GET /v1/traces rejects (422) any window wider than 7 days, skew included.
export const TRACE_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
export const TRACE_POLL_MS = 3000;

export function latestTraceWindow(now: Date): { start_time_from: string; start_time_to: string } {
  return {
    start_time_from: new Date(now.getTime() + FUTURE_SKEW_MS - TRACE_WINDOW_MS).toISOString(),
    start_time_to: new Date(now.getTime() + FUTURE_SKEW_MS).toISOString(),
  };
}

/**
 * Polls the project's most recent trace (a real GET /v1/traces through the
 * BFF, one row) until one exists, then stops. Powers the "waiting for your
 * first trace" states: they flip the moment a real trace is stored.
 */
export function useLatestTrace(): TraceSummary | null {
  const [trace, setTrace] = useState<TraceSummary | null>(null);

  useEffect(() => {
    if (trace) return;
    let cancelled = false;
    let timer: number | undefined;

    async function poll(): Promise<void> {
      try {
        const data = await fetchVigilProxy<TraceListResponse>("/api/vigil/traces", {
          ...latestTraceWindow(new Date()),
          limit: 1,
        });
        if (!cancelled && data.traces[0]) {
          setTrace(data.traces[0]);
          return;
        }
      } catch {
        // Transient failures just mean "not yet" -- keep polling.
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), TRACE_POLL_MS);
    }

    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [trace]);

  return trace;
}
