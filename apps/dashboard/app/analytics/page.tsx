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

// Without this, this page has no dynamic API to opt it out of static
// optimization, so Next prerenders it at build time -- freezing its CSP
// nonce into the served HTML instead of stamping the per-request one
// proxy.ts generates (see app/page.tsx's identical fix for the same
// React #412 streaming-script failure this causes).
export const dynamic = "force-dynamic";

export default function AnalyticsPage() {
  return <AnalyticsView />;
}
