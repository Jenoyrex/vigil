"use client";

import { useState } from "react";

import { cn } from "@/lib/cn";

/** Copies `value` to the clipboard on click; announces success via `aria-live`. */
export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  async function handleClick() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be denied by the browser; failing silently
      // (no copy happens) is preferable to throwing in a click handler.
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      aria-label={copied ? "Copied" : label}
      className={cn(
        "rounded px-1.5 py-0.5 text-xs font-medium text-muted transition-colors",
        "hover:bg-surface-hover hover:text-foreground",
      )}
    >
      <span aria-live="polite">{copied ? "Copied" : label}</span>
    </button>
  );
}
