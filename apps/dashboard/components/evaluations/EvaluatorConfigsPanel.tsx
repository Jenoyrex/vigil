"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Skeleton } from "@/components/ui/Skeleton";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import { VigilApiError, type EvaluatorConfigListResponse, type EvaluatorConfigOut } from "@/lib/api/types";
import { titleForStatus } from "@/lib/errorMessages";

import { EvaluatorConfigForm } from "./EvaluatorConfigForm";

interface FetchError {
  status: number;
  message: string;
}

function toFetchError(error: unknown): FetchError {
  if (error instanceof VigilApiError) return { status: error.status, message: error.message };
  return { status: 0, message: "Unable to load evaluator configurations. Please retry." };
}

/**
 * evaluator_name is an open string -- there is no catalog/list-of-installed-
 * evaluators endpoint (ADR 005 section 2/10, matching `span_type`'s
 * precedent) -- so "configure a new evaluator" is a free-text field, never
 * a hardcoded dropdown of today's two known names. That would silently
 * encode an assumption this API doesn't actually enforce and would need a
 * dashboard code change every time a new evaluator ships.
 */
export function EvaluatorConfigsPanel({
  initialConfigs,
  initialError,
}: {
  initialConfigs: EvaluatorConfigOut[] | null;
  initialError: FetchError | null;
}) {
  const [configs, setConfigs] = useState<EvaluatorConfigOut[]>(initialConfigs ?? []);
  const [status, setStatus] = useState<"idle" | "loading" | "error">(initialError ? "error" : "idle");
  const [error, setError] = useState<FetchError | null>(initialError);
  const [newEvaluatorName, setNewEvaluatorName] = useState("");
  const [addingName, setAddingName] = useState<string | null>(null);

  /** Retries the same GET /v1/evaluations/configs the initial server-side
   * render made (via the same /api/vigil proxy every other client-side
   * refetch in this app uses), so a transient load failure recovers in
   * place -- no full page reload required. */
  async function retry(): Promise<void> {
    setStatus("loading");
    setError(null);
    try {
      const result = await fetchVigilProxy<EvaluatorConfigListResponse>("/api/vigil/evaluations/configs");
      setConfigs(result.configs);
      setStatus("idle");
    } catch (err) {
      setStatus("error");
      setError(toFetchError(err));
    }
  }

  function handleSaved(config: EvaluatorConfigOut): void {
    setConfigs((previous) => {
      const existingIndex = previous.findIndex((c) => c.evaluator_name === config.evaluator_name);
      const next =
        existingIndex === -1
          ? [...previous, config]
          : previous.map((c, index) => (index === existingIndex ? config : c));
      return [...next].sort((a, b) => a.evaluator_name.localeCompare(b.evaluator_name));
    });
    // Only the add-form's own save should close the add-form -- saving an
    // unrelated already-configured evaluator while a new-evaluator name is
    // still being drafted (but not yet submitted) must never discard it.
    if (config.evaluator_name === addingName) {
      cancelAdding();
    }
  }

  function startAdding(): void {
    const trimmed = newEvaluatorName.trim();
    if (!trimmed) return;
    setAddingName(trimmed);
  }

  function cancelAdding(): void {
    setAddingName(null);
    setNewEvaluatorName("");
  }

  if (status === "loading") {
    return (
      <div className="space-y-3" role="status" aria-label="Loading">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (status === "error" && error) {
    return (
      <ErrorBanner title={titleForStatus(error.status)} message={error.message} onRetry={() => void retry()} />
    );
  }

  return (
    <div className="space-y-4">
      {configs.length === 0 && addingName === null ? (
        <EmptyState
          title="No evaluators configured"
          description="Evaluation is opt-in and off by default. Configure an evaluator to start scoring telemetry."
        />
      ) : (
        configs.map((config) => (
          <EvaluatorConfigForm
            key={config.evaluator_name}
            evaluatorName={config.evaluator_name}
            initialConfig={config}
            onSaved={handleSaved}
          />
        ))
      )}

      {addingName !== null ? (
        <EvaluatorConfigForm
          evaluatorName={addingName}
          initialConfig={null}
          onSaved={handleSaved}
          onCancel={cancelAdding}
        />
      ) : (
        <div className="flex flex-wrap items-end gap-2 rounded-lg border border-dashed border-border p-4">
          <label className="flex flex-1 flex-col gap-1 text-xs font-medium text-muted" htmlFor="new-evaluator-name">
            Evaluator name
            <input
              id="new-evaluator-name"
              type="text"
              placeholder="e.g. relevance, relevance_embedding"
              value={newEvaluatorName}
              onChange={(event) => setNewEvaluatorName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  startAdding();
                }
              }}
              className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground placeholder:text-muted"
            />
          </label>
          <Button type="button" variant="primary" onClick={startAdding} disabled={!newEvaluatorName.trim()}>
            Configure
          </Button>
        </div>
      )}
    </div>
  );
}
