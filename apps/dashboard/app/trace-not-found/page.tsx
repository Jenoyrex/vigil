import { notFound } from "next/navigation";

/**
 * Rewrite target for a trace `proxy.ts` has already confirmed doesn't
 * exist. Lives outside app/traces/** deliberately: that subtree's
 * app/traces/[traceId]/loading.tsx (and the app root's own loading
 * boundary) would wrap any nested route in an implicit Suspense boundary,
 * and once that boundary's fallback can stream, the response has already
 * committed to a 200 status before notFound() ever runs -- see the
 * `notFound()` API reference, "Calling notFound() after streaming has
 * started". This segment has no loading.tsx anywhere in its ancestry, so
 * notFound() here runs before any streaming starts: a real HTTP 404,
 * resolved to ./not-found.tsx (which re-exports the trace-detail page's
 * existing "Trace not found" UI, unchanged). See proxy.ts for the
 * existence check that rewrites here.
 */
export default function TraceNotFoundPage(): never {
  notFound();
}
