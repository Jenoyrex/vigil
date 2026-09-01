"use client";

import { Button } from "./Button";

/**
 * Standard error UI for every route's `error.tsx` and inline data-fetch
 * failures. `message` must already be a safe, user-facing string (the
 * upstream API's own `detail`, or a generic fallback) -- never a raw
 * Error/stack trace. See app/api/vigil/_lib/handleError.ts and
 * lib/api/vigilClient.ts, which are responsible for that sanitization
 * before an error ever reaches a component.
 */
export function ErrorBanner({
  title = "Something went wrong",
  message,
  onRetry,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col gap-2 rounded-lg border border-status-error-bg bg-status-error-bg/40 px-4 py-3 text-sm sm:flex-row sm:items-center sm:justify-between"
    >
      <div>
        <p className="font-medium text-status-error">{title}</p>
        <p className="text-foreground">{message}</p>
      </div>
      {onRetry ? (
        <Button variant="secondary" onClick={onRetry} className="shrink-0">
          Retry
        </Button>
      ) : null}
    </div>
  );
}
