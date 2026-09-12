import type { EvaluationJobStatus } from "@/lib/api/types";

import { Badge, type BadgeTone } from "../ui/Badge";

/**
 * `failed` and `dead_letter` share the `error` tone (both are failure
 * states an operator should notice) but stay visually distinguishable by
 * their own status text -- `dead_letter` is the terminal, exhausted-retries
 * state (ADR 005 section 1), `failed` is still retryable.
 */
const STATUS_TONE: Record<EvaluationJobStatus, BadgeTone> = {
  pending: "neutral",
  running: "accent",
  succeeded: "ok",
  failed: "error",
  dead_letter: "error",
};

export function EvaluationJobStatusBadge({ status }: { status: EvaluationJobStatus }) {
  return <Badge tone={STATUS_TONE[status]}>{status}</Badge>;
}
