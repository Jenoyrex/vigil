import type { InputHTMLAttributes } from "react";

import { cn } from "@/lib/cn";

/**
 * A native checkbox styled as a toggle switch via `accent-color`, not a
 * hand-built div-based switch -- consistent with how the rest of this app
 * prefers native control semantics (ExpandableSection's `<details>`,
 * TraceFilters' plain `<select>`) over reimplementing widget behavior the
 * browser already provides correctly (keyboard toggling, checked state,
 * screen-reader semantics). The first form-input primitive in this app --
 * see components/evaluations/EvaluatorConfigForm.tsx, its only caller.
 */
export function Toggle({
  label,
  className,
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, "type"> & { label: string }) {
  return (
    <label className={cn("inline-flex items-center gap-2 text-sm text-foreground", className)}>
      <input
        type="checkbox"
        className="h-4 w-8 shrink-0 cursor-pointer appearance-auto accent-accent disabled:cursor-not-allowed disabled:opacity-50"
        {...props}
      />
      {label}
    </label>
  );
}
