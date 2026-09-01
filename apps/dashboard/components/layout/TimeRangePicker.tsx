"use client";

import { TIME_RANGE_PRESETS, type TimeRangePreset } from "@/lib/time-range";
import { cn } from "@/lib/cn";

export function TimeRangePicker({
  value,
  onChange,
}: {
  value: TimeRangePreset;
  onChange: (preset: TimeRangePreset) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Time range"
      className="inline-flex rounded-md border border-border bg-surface p-0.5"
    >
      {TIME_RANGE_PRESETS.map((preset) => (
        <button
          key={preset.value}
          type="button"
          aria-pressed={value === preset.value}
          onClick={() => onChange(preset.value)}
          className={cn(
            "rounded px-2.5 py-1 text-xs font-medium transition-colors",
            value === preset.value
              ? "bg-accent text-accent-foreground"
              : "text-muted hover:text-foreground",
          )}
        >
          {preset.label}
        </button>
      ))}
    </div>
  );
}
