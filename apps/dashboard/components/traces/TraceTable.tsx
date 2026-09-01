import Link from "next/link";

import { StatusBadge } from "@/components/ui/StatusBadge";
import { Table, Tbody, Td, Th, Thead, Tr } from "@/components/ui/Table";
import type { TraceSummary } from "@/lib/api/types";
import { formatAbsoluteTime, formatCount, formatDuration, formatRelativeTime, truncateId } from "@/lib/format";

/** Columns: Trace ID, Root Operation, Start Time, Duration, Status, Span Count, Environment. */
export function TraceTable({ traces }: { traces: TraceSummary[] }) {
  return (
    <Table aria-label="Traces">
      <Thead>
        <Tr>
          <Th>Trace ID</Th>
          <Th>Root Operation</Th>
          <Th>Start Time</Th>
          <Th className="text-right">Duration</Th>
          <Th>Status</Th>
          <Th className="text-right">Spans</Th>
          <Th>Environment</Th>
        </Tr>
      </Thead>
      <Tbody>
        {traces.map((trace) => (
          <Tr key={trace.trace_id}>
            <Td>
              <Link
                href={`/traces/${trace.trace_id}?start=${encodeURIComponent(trace.start_time)}`}
                className="font-mono text-accent underline-offset-2 hover:underline"
                title={trace.trace_id}
              >
                {truncateId(trace.trace_id)}
              </Link>
            </Td>
            <Td className="max-w-xs truncate text-foreground" title={trace.root_span_name ?? undefined}>
              {trace.root_span_name ?? <span className="text-muted">—</span>}
            </Td>
            <Td>
              <time dateTime={trace.start_time} title={formatAbsoluteTime(trace.start_time)} className="text-muted">
                {formatRelativeTime(trace.start_time)}
              </time>
            </Td>
            <Td className="text-right font-mono tabular-nums">{formatDuration(trace.duration_ms)}</Td>
            <Td>
              <StatusBadge status={trace.status} />
            </Td>
            <Td className="text-right font-mono tabular-nums">{formatCount(trace.span_count)}</Td>
            <Td className="text-muted">{trace.environment}</Td>
          </Tr>
        ))}
      </Tbody>
    </Table>
  );
}
