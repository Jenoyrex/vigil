"use client";

import { useEffect } from "react";

import { ErrorBanner } from "@/components/ui/ErrorBanner";

/**
 * Root error boundary. Deliberately shows only a generic message: an error
 * caught here came from a thrown exception during render, not a handled
 * API failure (those are caught and shown inline by the pages themselves),
 * and Next.js redacts `error.message` in production for exactly the reason
 * this app cares about -- never surfacing internals to the browser. Only
 * `error.digest` (a safe correlation id, not the message) is logged.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("dashboard error boundary", error.digest ?? "(no digest)");
  }, [error]);

  return (
    <ErrorBanner
      title="Something went wrong"
      message="An unexpected error occurred while loading this page."
      onRetry={reset}
    />
  );
}
