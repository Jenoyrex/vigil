import { TracesExplorer } from "@/components/traces/TracesExplorer";
import { listTraces } from "@/lib/api/traces";
import { VigilApiError, type TraceListResponse } from "@/lib/api/types";
import { searchParamsToTraceFilters, TRACE_LIST_PAGE_SIZE } from "@/lib/search-params";
import { resolveTimeRange } from "@/lib/time-range";

interface FetchError {
  status: number;
  message: string;
}

export default async function TracesPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const filters = searchParamsToTraceFilters(await searchParams);
  const range = resolveTimeRange(filters.range);

  let initialData: TraceListResponse | null = null;
  let initialError: FetchError | null = null;

  try {
    initialData = await listTraces({
      start_time_from: range.start_time_from,
      start_time_to: range.start_time_to,
      environment: filters.environment,
      resource: filters.resource,
      has_error: filters.hasError,
      cursor: filters.cursor,
      limit: TRACE_LIST_PAGE_SIZE,
    });
  } catch (error) {
    initialError =
      error instanceof VigilApiError
        ? { status: error.status, message: error.message }
        : { status: 0, message: "Unable to load traces." };
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-foreground">Traces</h1>
      <TracesExplorer initialFilters={filters} initialData={initialData} initialError={initialError} />
    </div>
  );
}
