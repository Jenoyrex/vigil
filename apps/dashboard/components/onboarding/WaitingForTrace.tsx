"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useLatestTrace } from "@/lib/hooks/useLatestTrace";

/**
 * Shown instead of a page's charts/tables while the project has no traces:
 * says what to do next, and refreshes into the real view by itself as soon
 * as the first real trace arrives.
 */
export function WaitingForTrace({
  heading,
  isNewProject,
  children,
}: {
  heading: string;
  isNewProject: boolean;
  children?: React.ReactNode;
}) {
  const router = useRouter();
  const trace = useLatestTrace();

  useEffect(() => {
    if (trace) router.refresh();
  }, [trace, router]);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-foreground">{heading}</h1>
      <div className="rounded-lg border border-border bg-surface px-6 py-10">
        <div className="mx-auto max-w-xl space-y-4 text-center">
          <div className="flex items-center justify-center gap-2 text-sm font-medium text-foreground">
            <span className="relative flex h-2.5 w-2.5" aria-hidden>
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60 motion-reduce:animate-none" />
              <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-accent" />
            </span>
            <span role="status">
              {isNewProject ? "Waiting for your first trace" : "No traces in the last 7 days"}
            </span>
          </div>
          <p className="text-sm text-muted">
            {children ??
              "Connect your application and send an LLM request. Once Vigil receives a trace, it appears here automatically."}
          </p>
          <div className="flex flex-wrap items-center justify-center gap-2">
            <Link
              href="/connect"
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-foreground hover:opacity-90"
            >
              Connect your app
            </Link>
            <Link
              href="/settings"
              className="rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-hover"
            >
              API keys
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
