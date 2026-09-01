import { Table, Tbody, Td, Th, Thead, Tr } from "@/components/ui/Table";
import type { LlmUsageGroup } from "@/lib/api/types";
import { formatCost, formatCount } from "@/lib/format";

/** `Number()` below is used only to size a decorative "% of max" bar (a
 * magnitude comparison, not a sum) -- never to compute or display a cost
 * value itself. See lib/format.ts's formatCost for the string-only path
 * that actually renders each cost. */
function costMagnitude(value: string): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function LlmUsageGroupedTable({ groups, dimensionLabel }: { groups: LlmUsageGroup[]; dimensionLabel: string }) {
  const maxCost = Math.max(1e-9, ...groups.map((group) => costMagnitude(group.total_cost_usd)));

  return (
    <Table aria-label="LLM usage by group">
      <Thead>
        <Tr>
          <Th>{dimensionLabel}</Th>
          <Th className="text-right">Spans</Th>
          <Th className="text-right">Input Tokens</Th>
          <Th className="text-right">Output Tokens</Th>
          <Th className="text-right">Total Tokens</Th>
          <Th className="text-right">Cost</Th>
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
                  style={{ width: `${(costMagnitude(group.total_cost_usd) / maxCost) * 100}%` }}
                />
              </div>
            </Td>
            <Td className="text-right font-mono tabular-nums">{formatCount(group.llm_span_count)}</Td>
            <Td className="text-right font-mono tabular-nums">{formatCount(group.total_input_tokens)}</Td>
            <Td className="text-right font-mono tabular-nums">{formatCount(group.total_output_tokens)}</Td>
            <Td className="text-right font-mono tabular-nums">{formatCount(group.total_tokens)}</Td>
            <Td className="text-right font-mono tabular-nums">{formatCost(group.total_cost_usd)}</Td>
          </Tr>
        ))}
      </Tbody>
    </Table>
  );
}
