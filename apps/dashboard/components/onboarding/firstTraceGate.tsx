import "server-only";

import { hasRecentTrace, TRACE_LOOKBACK_DAYS } from "@/lib/api/traces";
import type { Project } from "@/lib/api/workspaceTypes";

import { WaitingForTrace } from "./WaitingForTrace";

/**
 * For data pages: when the current project has no traces to show, returns
 * the "waiting for your first trace" state to render instead of empty
 * charts and zero tiles; otherwise null. A failed check falls through to
 * the page itself, whose own error handling then reports the problem.
 */
export async function firstTraceGate(
  project: Project,
  heading: string,
  description?: string,
): Promise<React.ReactElement | null> {
  const hasTrace = await hasRecentTrace().catch(() => true);
  if (hasTrace) return null;
  const ageMs = Date.now() - new Date(project.created_at).getTime();
  return (
    <WaitingForTrace heading={heading} isNewProject={ageMs < TRACE_LOOKBACK_DAYS * 24 * 60 * 60 * 1000}>
      {description}
    </WaitingForTrace>
  );
}
