"use client";

import { useEffect } from "react";

import { ErrorBanner } from "@/components/ui/ErrorBanner";

export default function EvaluationsError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("evaluations page error boundary", error.digest ?? "(no digest)");
  }, [error]);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-foreground">Evaluations</h1>
      <ErrorBanner message="Unable to load evaluations right now." onRetry={reset} />
    </div>
  );
}
