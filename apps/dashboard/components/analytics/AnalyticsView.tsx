"use client";

import { useState } from "react";

import { cn } from "@/lib/cn";

import { LlmUsagePanel } from "./LlmUsagePanel";
import { TelemetryPanel } from "./TelemetryPanel";

type Tab = "telemetry" | "llm";

const TABS: { value: Tab; label: string }[] = [
  { value: "telemetry", label: "Telemetry" },
  { value: "llm", label: "LLM Usage" },
];

export function AnalyticsView() {
  const [tab, setTab] = useState<Tab>("telemetry");

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-foreground">Analytics</h1>

      <div role="tablist" aria-label="Analytics section" className="inline-flex rounded-md border border-border bg-surface p-0.5">
        {TABS.map((item) => (
          <button
            key={item.value}
            type="button"
            role="tab"
            id={`analytics-tab-${item.value}`}
            aria-selected={tab === item.value}
            aria-controls={`analytics-panel-${item.value}`}
            onClick={() => setTab(item.value)}
            className={cn(
              "rounded px-3 py-1.5 text-sm font-medium transition-colors",
              tab === item.value ? "bg-accent text-accent-foreground" : "text-muted hover:text-foreground",
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`analytics-panel-${tab}`} aria-labelledby={`analytics-tab-${tab}`}>
        {tab === "telemetry" ? <TelemetryPanel /> : <LlmUsagePanel />}
      </div>
    </div>
  );
}
