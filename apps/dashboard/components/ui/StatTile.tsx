import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export function StatTile({
  label,
  value,
  sublabel,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  sublabel?: ReactNode;
  tone?: "default" | "error";
}) {
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3">
      <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
      <p
        className={cn(
          "mt-1 font-mono text-2xl font-semibold tabular-nums",
          tone === "error" ? "text-status-error" : "text-foreground",
        )}
      >
        {value}
      </p>
      {sublabel ? <p className="mt-0.5 text-xs text-muted">{sublabel}</p> : null}
    </div>
  );
}
