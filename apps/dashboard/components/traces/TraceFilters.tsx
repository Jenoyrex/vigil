"use client";

import { TimeRangePicker } from "@/components/layout/TimeRangePicker";
import { FilterDropdown, type FilterDropdownOption } from "@/components/ui/FilterDropdown";
import type { TimeRangePreset } from "@/lib/time-range";

export type ErrorFilter = "all" | "error" | "ok";

export function TraceFilters({
  range,
  onRangeChange,
  environment,
  onEnvironmentChange,
  environmentOptions,
  resource,
  onResourceChange,
  resourceOptions,
  errorFilter,
  onErrorFilterChange,
}: {
  range: TimeRangePreset;
  onRangeChange: (range: TimeRangePreset) => void;
  environment: string | undefined;
  onEnvironmentChange: (value: string | undefined) => void;
  environmentOptions: FilterDropdownOption[];
  resource: string | undefined;
  onResourceChange: (value: string | undefined) => void;
  resourceOptions: FilterDropdownOption[];
  errorFilter: ErrorFilter;
  onErrorFilterChange: (value: ErrorFilter) => void;
}) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <TimeRangePicker value={range} onChange={onRangeChange} />
      <FilterDropdown
        label="Environment"
        value={environment}
        options={environmentOptions}
        onChange={onEnvironmentChange}
      />
      <FilterDropdown label="Resource" value={resource} options={resourceOptions} onChange={onResourceChange} />
      <div className="flex flex-col gap-1 text-xs font-medium text-muted">
        Status
        <select
          value={errorFilter}
          onChange={(event) => onErrorFilterChange(event.target.value as ErrorFilter)}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground"
        >
          <option value="all">All</option>
          <option value="error">Errors only</option>
          <option value="ok">No errors</option>
        </select>
      </div>
    </div>
  );
}
