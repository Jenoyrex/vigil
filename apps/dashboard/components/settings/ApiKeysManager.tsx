"use client";

import { useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { CopyButton } from "@/components/ui/CopyButton";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { TextField } from "@/components/ui/TextField";
import { formatRelativeTime } from "@/lib/format";
import { postJson } from "@/lib/api/postJson";
import type { ApiKeyCreated, ApiKeyOut } from "@/lib/api/workspaceTypes";

/** List, create (raw key revealed once) and revoke the current project's API keys. */
export function ApiKeysManager({ initialKeys, canManage }: { initialKeys: ApiKeyOut[]; canManage: boolean }) {
  const [keys, setKeys] = useState(initialKeys);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [revealed, setRevealed] = useState<ApiKeyCreated | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleCreate(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (creating || !name.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const created = await postJson<ApiKeyCreated>("/api/workspace/api-keys", { name });
      setRevealed(created);
      setKeys((current) => [created, ...current]);
      setName("");
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setCreating(false);
    }
  }

  async function handleRevoke(keyId: string): Promise<void> {
    setError(null);
    try {
      const updated = await postJson<ApiKeyOut>(`/api/workspace/api-keys/${encodeURIComponent(keyId)}/revoke`);
      setKeys((current) => current.map((key) => (key.id === keyId ? updated : key)));
      if (revealed?.id === keyId) setRevealed(null);
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setConfirmingId(null);
    }
  }

  return (
    <div className="space-y-4">
      {canManage ? (
        <form onSubmit={(event) => void handleCreate(event)} className="flex flex-wrap items-end gap-3">
          <div className="min-w-56 flex-1">
            <TextField
              label="New key name"
              placeholder="production-backend"
              maxLength={200}
              value={name}
              disabled={creating}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <Button type="submit" variant="primary" className="py-2" disabled={creating || !name.trim()}>
            {creating ? "Creating…" : "Create key"}
          </Button>
        </form>
      ) : null}

      {revealed ? (
        <div className="space-y-2 rounded-md border border-accent/40 bg-accent/5 p-3" role="status">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <code className="break-all font-mono text-sm text-foreground">{revealed.api_key}</code>
            <CopyButton value={revealed.api_key} label="Copy key" />
          </div>
          <p className="text-xs text-muted">
            <strong className="font-medium text-foreground">Copy “{revealed.name}” now — it won&apos;t be shown again.</strong>{" "}
            Use it only in server-side code; never commit it or expose it in browser code.
          </p>
        </div>
      ) : null}

      {error ? <ErrorBanner message={error} /> : null}

      {keys.length === 0 ? (
        <EmptyState
          title="No API keys yet"
          description="Create an API key to connect your application. Each key can send traces to this project only."
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-left text-sm">
            <thead className="bg-surface text-xs text-muted">
              <tr>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Key</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 font-medium">Last used</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {keys.map((key) => (
                <tr key={key.id}>
                  <td className="px-3 py-2 text-foreground">{key.name}</td>
                  <td className="px-3 py-2 font-mono text-xs text-muted">{key.key_prefix}.••••</td>
                  <td className="px-3 py-2 text-muted">{formatRelativeTime(key.created_at)}</td>
                  <td className="px-3 py-2 text-muted">
                    {key.last_used_at ? formatRelativeTime(key.last_used_at) : "Never"}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={key.status === "active" ? "ok" : "neutral"}>{key.status}</Badge>
                  </td>
                  <td className="px-3 py-2 text-right">
                    {canManage && key.status === "active" ? (
                      confirmingId === key.id ? (
                        <span className="inline-flex gap-1">
                          <Button variant="secondary" onClick={() => void handleRevoke(key.id)}>
                            Confirm revoke
                          </Button>
                          <Button variant="ghost" onClick={() => setConfirmingId(null)}>
                            Cancel
                          </Button>
                        </span>
                      ) : (
                        <Button variant="ghost" onClick={() => setConfirmingId(key.id)}>
                          Revoke
                        </Button>
                      )
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
