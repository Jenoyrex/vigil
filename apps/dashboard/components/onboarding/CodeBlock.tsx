import { CopyButton } from "@/components/ui/CopyButton";

/** A copyable code/command block. `copyValue` defaults to the displayed code. */
export function CodeBlock({
  code,
  label,
  copyValue,
}: {
  code: string;
  label?: string;
  copyValue?: string;
}) {
  return (
    <div className="overflow-hidden rounded-md border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <span className="font-mono text-xs text-muted">{label ?? ""}</span>
        <CopyButton value={copyValue ?? code} label="Copy" />
      </div>
      <pre className="overflow-x-auto px-3 py-2.5 font-mono text-xs leading-relaxed text-foreground">
        <code>{code}</code>
      </pre>
    </div>
  );
}
