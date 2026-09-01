import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export type BadgeTone = "ok" | "error" | "unknown" | "neutral" | "accent";

const TONE_CLASSES: Record<BadgeTone, string> = {
  ok: "bg-status-ok-bg text-status-ok",
  error: "bg-status-error-bg text-status-error",
  unknown: "bg-status-unknown-bg text-status-unknown",
  neutral: "bg-surface-hover text-muted",
  accent: "bg-accent/10 text-accent",
};

export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium leading-none",
        TONE_CLASSES[tone],
      )}
    >
      {children}
    </span>
  );
}
