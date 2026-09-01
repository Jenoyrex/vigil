import { AnalyticsView } from "@/components/analytics/AnalyticsView";

/**
 * Each panel manages its own range/filter/mode state and fetches directly
 * via the /api/vigil/** proxy on mount and on every control change (see
 * TelemetryPanel/LlmUsagePanel) -- unlike Overview/Traces/Trace-detail,
 * this page does not do a server-side initial fetch, since Analytics is an
 * explore tool with many independent parameter combinations (2 tabs x 3
 * modes) where instant client-side interactivity matters more than a
 * zero-flash first paint.
 */
export default function AnalyticsPage() {
  return <AnalyticsView />;
}
