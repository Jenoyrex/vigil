import { Table, Tbody, Td, Th, Thead, Tr } from "@/components/ui/Table";
import type { SpanAnalyticsGroup } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { formatCount, formatDuration, formatPercent } from "@/lib/format";

/** Top-50 ranked table with a lightweight in-row "% of max" bar, instead of
 * a separate chart component -- keeps the grouped view dependency-free. */
export function SpanGroupedTable({ groups, dimensionLabel }: { groups: SpanAnalyticsGroup[]; dimensionLabel: string }) {
  const maxCount = Math.max(1, ...groups.map((group) => group.span_count));

  return (
    <Table aria-label="Span analytics by group">
      <Thead>
        <Tr>
          <Th>{dimensionLabel}</Th>
          <Th className="text-right">Spans</Th>
          <Th className="text-right">Error Rate</Th>
          <Th className="text-right">p50</Th>
          <Th className="text-right">p90</Th>
          <Th className="text-right">p99</Th>
        </Tr>
      </Thead>
      <Tbody>
        {groups.map((group) => (
          <Tr key={group.value}>
            <Td>
              <span className="text-foreground">{group.value}</span>
              <div className="mt-1 h-1 w-24 rounded bg-surface-hover" aria-hidden="true">
                <div
                  className="h-1 rounded bg-accent"
                  style={{ width: `${(group.span_count / maxCount) * 100}%` }}
                />
              </div>
            </Td>
            <Td className="text-right font-mono tabular-nums">{formatCount(group.span_count)}</Td>
            <Td
              className={cn(
                "text-right font-mono tabular-nums",
                group.error_span_count > 0 && "text-status-error",
              )}
            >
              {formatPercent(group.error_rate)}
            </Td>
            <Td className="text-right font-mono tabular-nums">{formatDuration(group.latency_ms.p50)}</Td>
            <Td className="text-right font-mono tabular-nums">{formatDuration(group.latency_ms.p90)}</Td>
            <Td className="text-right font-mono tabular-nums">{formatDuration(group.latency_ms.p99)}</Td>
          </Tr>
        ))}
      </Tbody>
    </Table>
  );
}
