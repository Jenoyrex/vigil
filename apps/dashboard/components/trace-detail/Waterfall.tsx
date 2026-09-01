"use client";

import { cn } from "@/lib/cn";
import { formatDuration } from "@/lib/format";
import { computeWaterfallPosition, flattenSpanTree, type SpanTreeNode } from "@/lib/tree";

const STATUS_BAR_CLASS: Record<string, string> = {
  ok: "bg-status-ok",
  error: "bg-status-error",
  unset: "bg-status-unknown",
};

/**
 * One row per span, indented by tree depth, with a horizontal bar
 * positioned/sized proportionally to the span's timing within the whole
 * trace. Plain buttons rather than a `role="tree"` widget: full keyboard
 * reachability (Tab) and activation (Enter/Space) come from native button
 * semantics without half-implementing roving-tabindex arrow-key navigation
 * a real ARIA tree widget would require for correctness.
 */
export function Waterfall({
  roots,
  traceStartMs,
  traceDurationMs,
  selectedSpanId,
  onSelect,
}: {
  roots: SpanTreeNode[];
  traceStartMs: number;
  traceDurationMs: number;
  selectedSpanId: string | null;
  onSelect: (spanId: string) => void;
}) {
  const rows = flattenSpanTree(roots);

  return (
    <div className="rounded-lg border border-border">
      {rows.map((node) => {
        const position = computeWaterfallPosition(node.span, traceStartMs, traceDurationMs);
        const selected = node.span.span_id === selectedSpanId;
        const label = `${node.span.name}, ${node.span.span_type}, status ${node.span.status}, duration ${formatDuration(node.span.duration_ms)}`;

        return (
          <button
            key={node.span.span_id}
            type="button"
            aria-pressed={selected}
            aria-label={label}
            onClick={() => onSelect(node.span.span_id)}
            className={cn(
              "flex w-full items-center gap-2 border-b border-border px-2 py-1.5 text-left text-xs last:border-b-0 hover:bg-surface-hover",
              selected && "bg-accent/10",
            )}
          >
            <span
              className="flex w-64 shrink-0 items-center gap-1 overflow-hidden"
              style={{ paddingLeft: `${node.depth * 14}px` }}
            >
              {node.parentMissing ? (
                <span title="Parent span not loaded (trace was truncated)" className="shrink-0 text-status-unknown">
                  ⚠
                </span>
              ) : null}
              <span className="truncate font-medium text-foreground">{node.span.name}</span>
              <span className="shrink-0 font-mono text-muted">{node.span.span_type}</span>
            </span>
            <span className="relative h-4 min-w-24 flex-1 rounded bg-surface" aria-hidden="true">
              <span
                className={cn("absolute top-0 h-4 rounded", STATUS_BAR_CLASS[node.span.status])}
                style={{ left: `${position.leftPercent}%`, width: `${position.widthPercent}%` }}
              />
            </span>
            <span className="w-16 shrink-0 text-right font-mono tabular-nums text-muted">
              {formatDuration(node.span.duration_ms)}
            </span>
          </button>
        );
      })}
    </div>
  );
}
