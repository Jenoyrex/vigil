"use client";

import { cn } from "@/lib/cn";

export interface ModeOption<T extends string> {
  value: T;
  label: string;
}

/**
 * A single selected mode determines exactly one of `group_by`/`bucket` --
 * the two can never both be set, because there is only one control that
 * picks one or the other (or neither, for "Totals"). This is how the UI
 * makes the backend's group_by/bucket mutual-exclusivity rule impossible
 * to violate, rather than just handling the resulting 422.
 */
export function ModeSelector<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: ModeOption<T>[];
  onChange: (value: T) => void;
}) {
  return (
    <div role="group" aria-label="View mode" className="inline-flex rounded-md border border-border bg-surface p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className={cn(
            "rounded px-2.5 py-1 text-xs font-medium transition-colors",
            value === option.value ? "bg-accent text-accent-foreground" : "text-muted hover:text-foreground",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
