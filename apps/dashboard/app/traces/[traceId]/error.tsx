"use client";

import { useEffect } from "react";

import { ErrorBanner } from "@/components/ui/ErrorBanner";

export default function TraceDetailError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("trace detail error boundary", error.digest ?? "(no digest)");
  }, [error]);

  return <ErrorBanner message="Unable to load this trace right now." onRetry={reset} />;
}
