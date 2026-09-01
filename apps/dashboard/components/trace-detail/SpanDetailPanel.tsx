"use client";

import { useState, type ReactNode } from "react";

import { CopyButton } from "@/components/ui/CopyButton";
import { ExpandableSection } from "@/components/ui/ExpandableSection";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { SpanEvent, SpanOut } from "@/lib/api/types";
import { formatAbsoluteTime, formatBytes, formatCost, formatDuration, formatNullableCount } from "@/lib/format";

/** Even once a section is expanded, cap the rendered text so a pathological
 * near-64KB blob can't freeze the tab -- a manual "show full" toggle
 * reveals the rest on request. */
const INLINE_PREVIEW_LIMIT = 20_000;

function DetailRow({ label, children, full = false }: { label: string; children: ReactNode; full?: boolean }) {
  return (
    <div className={full ? "col-span-2" : undefined}>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="text-foreground">{children}</dd>
    </div>
  );
}

function tryPrettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function PayloadContent({ value }: { value: string | null }) {
  const [showFull, setShowFull] = useState(false);

  if (value === null) return <p className="text-muted">—</p>;

  const isLong = value.length > INLINE_PREVIEW_LIMIT;
  const display = showFull || !isLong ? value : value.slice(0, INLINE_PREVIEW_LIMIT);

  return (
    <div>
      <pre className="whitespace-pre-wrap break-words">{tryPrettyJson(display)}</pre>
      {isLong && !showFull ? (
        <button
          type="button"
          onClick={() => setShowFull(true)}
          className="mt-2 text-accent underline-offset-2 hover:underline"
        >
          Show full content ({formatBytes(value.length)})
        </button>
      ) : null}
    </div>
  );
}

function AttributesTable({ attributes }: { attributes: Record<string, string> }) {
  const entries = Object.entries(attributes);
  if (entries.length === 0) return <p className="text-muted">No attributes.</p>;
  return (
    <table className="w-full text-left">
      <tbody>
        {entries.map(([key, value]) => (
          <tr key={key} className="align-top">
            <td className="w-1/3 pr-2 text-muted">{key}</td>
            <td className="break-words text-foreground">{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EventsList({ events }: { events: SpanEvent[] }) {
  if (events.length === 0) return <p className="text-muted">No events.</p>;
  return (
    <ul className="space-y-2">
      {events.map((event, index) => (
        <li key={index} className="border-b border-border pb-2 last:border-b-0">
          <p className="font-medium text-foreground">{event.name}</p>
          <p className="text-muted" title={event.time}>
            {formatAbsoluteTime(event.time)}
          </p>
          {Object.keys(event.attributes).length > 0 ? <AttributesTable attributes={event.attributes} /> : null}
        </li>
      ))}
    </ul>
  );
}

export function SpanDetailPanel({ span }: { span: SpanOut }) {
  return (
    <div className="space-y-4 rounded-lg border border-border p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-medium text-foreground">{span.name}</p>
          <p className="font-mono text-xs text-muted">{span.span_id}</p>
        </div>
        <CopyButton value={span.span_id} label="Copy span ID" />
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <DetailRow label="Status">
          <StatusBadge status={span.status} />
        </DetailRow>
        <DetailRow label="Type">{span.span_type}</DetailRow>
        {span.status_message ? (
          <DetailRow label="Status message" full>
            {span.status_message}
          </DetailRow>
        ) : null}
        <DetailRow label="Duration">
          <span className="font-mono">{formatDuration(span.duration_ms)}</span>
        </DetailRow>
        <DetailRow label="Environment">{span.environment}</DetailRow>
        <DetailRow label="Start">
          <span title={span.start_time}>{formatAbsoluteTime(span.start_time)}</span>
        </DetailRow>
        <DetailRow label="Release">{span.release ?? "—"}</DetailRow>
        <DetailRow label="End">
          <span title={span.end_time}>{formatAbsoluteTime(span.end_time)}</span>
        </DetailRow>
      </dl>

      {span.llm_provider ? (
        <div className="rounded-md border border-border bg-surface p-3">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">LLM</p>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <DetailRow label="Provider">{span.llm_provider}</DetailRow>
            <DetailRow label="Model">{span.llm_model ?? "—"}</DetailRow>
            <DetailRow label="Input tokens">
              <span className="font-mono">{formatNullableCount(span.llm_input_tokens)}</span>
            </DetailRow>
            <DetailRow label="Output tokens">
              <span className="font-mono">{formatNullableCount(span.llm_output_tokens)}</span>
            </DetailRow>
            <DetailRow label="Total tokens">
              <span className="font-mono">{formatNullableCount(span.llm_total_tokens)}</span>
            </DetailRow>
            <DetailRow label="Cost">
              <span className="font-mono">{formatCost(span.llm_cost_usd)}</span>
            </DetailRow>
          </dl>
        </div>
      ) : null}

      <div className="space-y-2">
        <ExpandableSection
          title="Input"
          sizeHint={formatBytes(span.input_size_bytes)}
          truncated={span.input_truncated}
          copyValue={span.input ?? undefined}
        >
          <PayloadContent value={span.input} />
        </ExpandableSection>
        <ExpandableSection
          title="Output"
          sizeHint={formatBytes(span.output_size_bytes)}
          truncated={span.output_truncated}
          copyValue={span.output ?? undefined}
        >
          <PayloadContent value={span.output} />
        </ExpandableSection>
        <ExpandableSection
          title="Attributes"
          sizeHint={`${Object.keys(span.attributes).length}`}
          truncated={span.attributes_truncated}
          copyValue={JSON.stringify(span.attributes, null, 2)}
        >
          <AttributesTable attributes={span.attributes} />
        </ExpandableSection>
        <ExpandableSection
          title="Events"
          sizeHint={`${span.events.length}`}
          truncated={span.events_truncated}
          copyValue={JSON.stringify(span.events, null, 2)}
        >
          <EventsList events={span.events} />
        </ExpandableSection>
      </div>
    </div>
  );
}
