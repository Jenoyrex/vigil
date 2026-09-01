"use client";

import { useId } from "react";

export interface FilterDropdownOption {
  value: string;
  label: string;
}

/**
 * A native `<select>` rather than a custom combobox: full keyboard
 * navigation, screen-reader semantics, and mobile picker UI come for free,
 * with zero JS and zero extra dependency (see the dashboard design's
 * accessibility-primitives decision).
 */
export function FilterDropdown({
  label,
  value,
  options,
  onChange,
  placeholder = "All",
  disabled = false,
}: {
  label: string;
  value: string | undefined;
  options: FilterDropdownOption[];
  onChange: (value: string | undefined) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const selectId = useId();

  return (
    <label htmlFor={selectId} className="flex flex-col gap-1 text-xs font-medium text-muted">
      {label}
      <select
        id={selectId}
        value={value ?? ""}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
        className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground disabled:opacity-50"
      >
        <option value="">{placeholder}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
