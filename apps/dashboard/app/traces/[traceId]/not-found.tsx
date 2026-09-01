import Link from "next/link";

export default function TraceNotFound() {
  return (
    <div className="space-y-2">
      <h1 className="text-lg font-semibold text-foreground">Trace not found</h1>
      <p className="text-sm text-muted">
        This trace ID may be incorrect, belong to a different project, or fall outside the 30-day
        retention window.
      </p>
      <Link href="/traces" className="text-sm text-accent underline-offset-2 hover:underline">
        Back to traces
      </Link>
    </div>
  );
}
