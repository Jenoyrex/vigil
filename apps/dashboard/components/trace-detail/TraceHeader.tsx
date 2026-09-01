import type { ReactNode } from "react";

import { CopyButton } from "@/components/ui/CopyButton";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { TraceDetailResponse } from "@/lib/api/types";
import { formatAbsoluteTime, formatCount, formatDuration } from "@/lib/format";

function HeaderStat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className="text-foreground">{children}</dd>
    </div>
  );
}

export function TraceHeader({
  trace,
  environment,
}: {
  trace: TraceDetailResponse;
  /** Trace-detail has no top-level environment field -- the caller derives
   * this from the root span (see TraceDetailView), since that's the only
   * place the data genuinely exists. */
  environment: string | undefined;
}) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="break-all font-mono text-base font-semibold text-foreground">{trace.trace_id}</h1>
        <CopyButton value={trace.trace_id} label="Copy trace ID" />
        <StatusBadge status={trace.status} />
      </div>

      <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
        <HeaderStat label="Start">
          <span title={trace.start_time}>{formatAbsoluteTime(trace.start_time)}</span>
        </HeaderStat>
        <HeaderStat label="Duration">
          <span className="font-mono">{formatDuration(trace.duration_ms)}</span>
        </HeaderStat>
        <HeaderStat label="Spans">
          <span className="font-mono">{formatCount(trace.span_count)}</span>
        </HeaderStat>
        <HeaderStat label="Environment">{environment ?? "—"}</HeaderStat>
      </dl>

      {trace.truncated ? (
        <div
          role="status"
          className="rounded-md border border-status-unknown-bg bg-status-unknown-bg/60 px-3 py-2 text-sm text-foreground"
        >
          Showing {formatCount(trace.span_count)} of {formatCount(trace.total_span_count)} spans.
        </div>
      ) : null}
    </div>
  );
}
