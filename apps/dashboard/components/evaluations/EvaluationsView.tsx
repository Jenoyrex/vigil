"use client";

import { useState } from "react";

import type { EvaluationJobListResponse, EvaluatorConfigOut } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import type { EvaluationJobFilters } from "@/lib/evaluationJobFilters";

import { EvaluationJobsExplorer } from "./EvaluationJobsExplorer";
import { EvaluatorConfigsPanel } from "./EvaluatorConfigsPanel";

type Tab = "config" | "jobs";

interface FetchError {
  status: number;
  message: string;
}

const TABS: { value: Tab; label: string }[] = [
  { value: "config", label: "Configuration" },
  { value: "jobs", label: "Jobs" },
];

/** Updates the URL's `tab` param in place, preserving every other param
 * (in practice, the Jobs tab's own filter/cursor params, whichever this tab
 * switch is leaving behind or returning to) -- switching tabs must never
 * clobber state a still-mounted panel is tracking. Mirrors the
 * `window.history.replaceState` convention every other client-side filter
 * update in this app already uses (e.g. EvaluationJobsExplorer's own
 * `syncUrl`), just scoped to one param instead of rebuilding the whole
 * query string. */
function syncTabParam(tab: Tab): void {
  const params = new URLSearchParams(window.location.search);
  params.set("tab", tab);
  window.history.replaceState(null, "", `/evaluations?${params.toString()}`);
}

/**
 * Both tabs' initial data is fetched server-side up front (app/evaluations/page.tsx)
 * rather than deferred to client-mount-per-tab the way Analytics does --
 * unlike Analytics's combinatorially many parameter combinations (2 tabs x
 * 3 modes x group-by/bucket), `GET /v1/evaluations/configs` and
 * `GET /v1/evaluations/jobs` are each one cheap, bounded, project-scoped
 * query, so prefetching both costs little and makes switching tabs instant
 * (no flash) for whichever tab isn't the default.
 *
 * Both panels stay mounted for the lifetime of this component -- the
 * inactive one is hidden via the native `hidden` attribute, never
 * conditionally unmounted. Unmounting would reset EvaluationJobsExplorer's
 * filters/cursor/fetched-data state (and EvaluatorConfigsPanel's in-progress
 * "add evaluator" draft) every time the user switches away and back, while
 * the URL kept whatever jobs filters were last applied -- visibly
 * inconsistent with what re-rendered. `hidden` removes the inactive panel
 * from layout and the accessibility tree without discarding its React
 * state, and neither panel fetches anything merely by being mounted (both
 * only fetch in response to a user action), so keeping both mounted costs
 * nothing extra over conditional rendering.
 */
export function EvaluationsView({
  initialTab,
  initialConfigs,
  initialConfigsError,
  initialJobFilters,
  initialJobs,
  initialJobsError,
}: {
  initialTab: Tab;
  initialConfigs: EvaluatorConfigOut[] | null;
  initialConfigsError: FetchError | null;
  initialJobFilters: EvaluationJobFilters;
  initialJobs: EvaluationJobListResponse | null;
  initialJobsError: FetchError | null;
}) {
  const [tab, setTab] = useState<Tab>(initialTab);

  function selectTab(next: Tab): void {
    setTab(next);
    syncTabParam(next);
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-foreground">Evaluations</h1>

      <div
        role="tablist"
        aria-label="Evaluations section"
        className="inline-flex rounded-md border border-border bg-surface p-0.5"
      >
        {TABS.map((item) => (
          <button
            key={item.value}
            type="button"
            role="tab"
            id={`evaluations-tab-${item.value}`}
            aria-selected={tab === item.value}
            aria-controls={`evaluations-panel-${item.value}`}
            onClick={() => selectTab(item.value)}
            className={cn(
              "rounded px-3 py-1.5 text-sm font-medium transition-colors",
              tab === item.value ? "bg-accent text-accent-foreground" : "text-muted hover:text-foreground",
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id="evaluations-panel-config"
        aria-labelledby="evaluations-tab-config"
        hidden={tab !== "config"}
      >
        <EvaluatorConfigsPanel initialConfigs={initialConfigs} initialError={initialConfigsError} />
      </div>
      <div
        role="tabpanel"
        id="evaluations-panel-jobs"
        aria-labelledby="evaluations-tab-jobs"
        hidden={tab !== "jobs"}
      >
        <EvaluationJobsExplorer
          initialFilters={initialJobFilters}
          initialData={initialJobs}
          initialError={initialJobsError}
        />
      </div>
    </div>
  );
}
