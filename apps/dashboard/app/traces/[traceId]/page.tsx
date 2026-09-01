import { notFound } from "next/navigation";
import { Suspense } from "react";

import { TraceDetailView } from "@/components/trace-detail/TraceDetailView";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { titleForStatus } from "@/lib/errorMessages";
import { Skeleton } from "@/components/ui/Skeleton";
import { getTrace } from "@/lib/api/traces";
import { deriveStartDate } from "@/lib/traceStartDate";
import { VigilApiError, type TraceDetailResponse } from "@/lib/api/types";

export default async function TraceDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ traceId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { traceId } = await params;
  const resolvedSearchParams = await searchParams;
  const startParam =
    typeof resolvedSearchParams.start === "string" ? resolvedSearchParams.start : undefined;

  // Fetching is kept out of the JSX-returning branches below (rather than
  // `return <.../>` directly inside this try) so a render-time error in a
  // child component is never mistakenly assumed to be caught here -- only
  // the awaited fetch itself is.
  let trace: TraceDetailResponse;
  try {
    trace = await getTrace(traceId, { start_date: deriveStartDate(startParam) });
  } catch (error) {
    if (error instanceof VigilApiError) {
      if (error.status === 404) notFound();
      return <ErrorBanner title={titleForStatus(error.status)} message={error.message} />;
    }
    throw error;
  }

  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <TraceDetailView trace={trace} />
    </Suspense>
  );
}
