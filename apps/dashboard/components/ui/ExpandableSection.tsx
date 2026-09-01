"use client";

import type { ReactNode } from "react";

import { Badge } from "./Badge";
import { CopyButton } from "./CopyButton";

/**
 * A collapsed-by-default content block for potentially-large span content
 * (input/output/attributes/events) -- see the trace-detail design's payload
 * safety requirement. Built on native `<details>`/`<summary>`: keyboard
 * toggling (Enter/Space when focused) and screen-reader disclosure
 * semantics come from the browser, not hand-rolled ARIA state management.
 * The body is only ever in the DOM when the section exists, but stays
 * visually and semantically collapsed (and thus not read by default by
 * assistive tech) until opened.
 */
export function ExpandableSection({
  title,
  sizeHint,
  truncated = false,
  copyValue,
  defaultOpen = false,
  children,
}: {
  title: string;
  sizeHint?: string;
  truncated?: boolean;
  copyValue?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details
      className="group rounded-md border border-border [&::details-content]:block"
      open={defaultOpen}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-sm font-medium marker:content-none [&::-webkit-details-marker]:hidden">
        <span className="flex flex-wrap items-center gap-2">
          <svg
            aria-hidden="true"
            viewBox="0 0 16 16"
            className="h-3 w-3 shrink-0 fill-none stroke-muted stroke-2 transition-transform group-open:rotate-90"
          >
            <path d="M5 3l6 5-6 5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span>{title}</span>
          {sizeHint ? <span className="font-mono text-xs font-normal text-muted">{sizeHint}</span> : null}
          {truncated ? <Badge tone="unknown">truncated during ingestion</Badge> : null}
        </span>
        {copyValue ? <CopyButton value={copyValue} /> : null}
      </summary>
      <div className="max-h-80 overflow-auto border-t border-border px-3 py-2 font-mono text-xs">
        {children}
      </div>
    </details>
  );
}
