import type { InputHTMLAttributes } from "react";

import { cn } from "@/lib/cn";

/** Labeled text input used by every form in the app (login, signup, onboarding, settings). */
export function TextField({
  label,
  hint,
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }) {
  return (
    <label className="flex flex-col gap-1.5 text-sm font-medium text-foreground">
      {label}
      <input
        className={cn(
          "rounded-md border border-border bg-background px-3 py-2 text-sm font-normal text-foreground",
          "placeholder:text-muted/70 focus:border-accent disabled:cursor-not-allowed disabled:opacity-50",
          className,
        )}
        {...props}
      />
      {hint ? <span className="text-xs font-normal text-muted">{hint}</span> : null}
    </label>
  );
}
