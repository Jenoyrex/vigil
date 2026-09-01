"use client";

import { useEffect, useState } from "react";

import { fetchVigilProxy } from "@/lib/api/browserClient";
import type { SpanAnalyticsResponse, SpanGroupBy } from "@/lib/api/types";
import type { ResolvedTimeRange } from "@/lib/time-range";

/**
 * Populates a filter dropdown's options from the *observed* values of a
 * dimension in the current time window, via
 * GET /v1/analytics/spans?group_by=<dimension> -- there is no "list
 * distinct environments/resources" endpoint, so this reuses the existing
 * grouped-analytics response instead of inventing one (see the approved
 * dashboard design, "Overview" filters).
 *
 * Best-effort: a failure here degrades to an empty option list (the
 * dropdown still works as free-form "no filter") rather than blocking the
 * page or surfacing its own error UI.
 */
export function useDimensionOptions(dimension: SpanGroupBy, range: ResolvedTimeRange): string[] {
  const [options, setOptions] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;

    fetchVigilProxy<SpanAnalyticsResponse>("/api/vigil/analytics/spans", {
      group_by: dimension,
      start_time_from: range.start_time_from,
      start_time_to: range.start_time_to,
    })
      .then((data) => {
        if (!cancelled) setOptions((data.groups ?? []).map((group) => group.value));
      })
      .catch(() => {
        if (!cancelled) setOptions([]);
      });

    return () => {
      cancelled = true;
    };
  }, [dimension, range.start_time_from, range.start_time_to]);

  return options;
}
