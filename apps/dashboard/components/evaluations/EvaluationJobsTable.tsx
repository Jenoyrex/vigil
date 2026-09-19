import Link from "next/link";

import { CopyButton } from "@/components/ui/CopyButton";
import { Table, Tbody, Td, Th, Thead, Tr } from "@/components/ui/Table";
import type { EvaluationJobOut } from "@/lib/api/types";
import { formatAbsoluteTime, formatCount, formatRelativeTime, truncateId } from "@/lib/format";

import { EvaluationJobStatusBadge } from "./EvaluationJobStatusBadge";

/** Columns: Span, Evaluator, Status, Attempts, Last Error, Created. */
export function EvaluationJobsTable({ jobs }: { jobs: EvaluationJobOut[] }) {
  return (
    <Table aria-label="Evaluation jobs">
      <Thead>
        <Tr>
          <Th>Span</Th>
          <Th>Evaluator</Th>
          <Th>Status</Th>
          <Th className="text-right">Attempts</Th>
          <Th>Last Error</Th>
          <Th>Created</Th>
        </Tr>
      </Thead>
      <Tbody>
        {jobs.map((job) => (
          <Tr key={job.id}>
            <Td>
              <div className="flex items-center gap-1 font-mono text-xs" title={`${job.trace_id} / ${job.span_id}`}>
                <Link
                  href={`/traces/${job.trace_id}?span=${job.span_id}&start=${encodeURIComponent(job.created_at)}`}
                  className="text-accent underline-offset-2 hover:underline"
                >
                  {truncateId(job.span_id, 6, 4)}
                </Link>
                <CopyButton value={job.span_id} label="Copy span ID" />
              </div>
            </Td>
            <Td className="text-foreground">
              {job.evaluator_name}
              <span className="ml-1 font-mono text-xs text-muted">{job.evaluator_version}</span>
            </Td>
            <Td>
              <EvaluationJobStatusBadge status={job.status} />
            </Td>
            <Td className="text-right font-mono tabular-nums">
              {formatCount(job.attempt_count)} / {formatCount(job.max_retries)}
            </Td>
            <Td className="max-w-xs truncate text-muted" title={job.last_error ?? undefined}>
              {job.last_error ?? "—"}
            </Td>
            <Td>
              <time dateTime={job.created_at} title={formatAbsoluteTime(job.created_at)} className="text-muted">
                {formatRelativeTime(job.created_at)}
              </time>
            </Td>
          </Tr>
        ))}
      </Tbody>
    </Table>
  );
}
