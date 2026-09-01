import type { SpanStatus, TraceStatus } from "@/lib/api/types";

import { Badge, type BadgeTone } from "./Badge";

const STATUS_TONE: Record<TraceStatus | SpanStatus, BadgeTone> = {
  ok: "ok",
  error: "error",
  unknown: "unknown",
  unset: "unknown",
};

export function StatusBadge({ status }: { status: TraceStatus | SpanStatus }) {
  return <Badge tone={STATUS_TONE[status]}>{status}</Badge>;
}
