"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/Button";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Toggle } from "@/components/ui/Toggle";
import { fetchVigilProxy } from "@/lib/api/browserClient";
import {
  VigilApiError,
  type EvaluatorConfigOut,
  type EvaluatorConfigUpsertRequest,
} from "@/lib/api/types";
import { titleForStatus } from "@/lib/errorMessages";

interface FormError {
  status: number;
  message: string;
}

function toFormError(error: unknown): FormError {
  if (error instanceof VigilApiError) return { status: error.status, message: error.message };
  return { status: 0, message: "Unable to save this configuration. Please retry." };
}

/**
 * Create-or-update form for one evaluator's configuration -- the customer-
 * facing enable/disable mechanism (ADR 005 section 10). `evaluatorName` is
 * fixed for the lifetime of one form instance: the API addresses a config
 * by evaluator_name in the URL path, so "renaming" isn't a PUT, it's a
 * separate config under a different name -- EvaluatorConfigsPanel handles
 * that distinction by mounting a fresh form per name, not by making the
 * name editable here.
 *
 * `PUT /v1/evaluations/configs/{evaluator_name}` is full-replace: every
 * submit sends all four fields, never a partial patch, matching the API's
 * own documented semantics exactly (an omitted field there resets to its
 * default, it does not preserve a prior value).
 */
export function EvaluatorConfigForm({
  evaluatorName,
  initialConfig,
  onSaved,
  onCancel,
}: {
  evaluatorName: string;
  initialConfig: EvaluatorConfigOut | null;
  onSaved: (config: EvaluatorConfigOut) => void;
  onCancel?: () => void;
}) {
  const [enabled, setEnabled] = useState(initialConfig?.enabled ?? false);
  const [samplingRate, setSamplingRate] = useState(String(initialConfig?.sampling_rate ?? 0.1));
  const [threshold, setThreshold] = useState(
    initialConfig?.threshold === null || initialConfig?.threshold === undefined
      ? ""
      : String(initialConfig.threshold),
  );
  const [maxRetries, setMaxRetries] = useState(String(initialConfig?.max_retries ?? 3));
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [error, setError] = useState<FormError | null>(null);

  /** Any edit after a save/error clears that stale status -- the badge
   * should only ever describe the form's state as of its own last change. */
  function edited<T>(setter: (value: T) => void) {
    return (value: T) => {
      if (status !== "saving") {
        setStatus("idle");
        setError(null);
      }
      setter(value);
    };
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (status === "saving") return; // prevent duplicate submission while a save is in flight

    const body: EvaluatorConfigUpsertRequest = {
      enabled,
      sampling_rate: samplingRate.trim() === "" ? 0.1 : Number(samplingRate),
      threshold: threshold.trim() === "" ? null : Number(threshold),
      max_retries: maxRetries.trim() === "" ? 3 : Number(maxRetries),
    };

    setStatus("saving");
    setError(null);
    try {
      const result = await fetchVigilProxy<EvaluatorConfigOut>(
        `/api/vigil/evaluations/configs/${encodeURIComponent(evaluatorName)}`,
        undefined,
        { method: "PUT", body },
      );
      setStatus("saved");
      onSaved(result);
    } catch (err) {
      setStatus("error");
      setError(toFormError(err));
    }
  }

  const saving = status === "saving";
  // Sanitized to a safe id fragment -- evaluator_name is an open,
  // customer-entered string (no catalog), so it can't be used as an id
  // verbatim (e.g. it may contain spaces).
  const legendId = `evaluator-config-legend-${evaluatorName.replace(/[^a-zA-Z0-9_-]/g, "-")}`;

  return (
    <form
      onSubmit={(event) => void handleSubmit(event)}
      aria-labelledby={legendId}
      className="space-y-3 rounded-lg border border-border p-4"
    >
      <fieldset className="m-0 min-w-0 border-0 p-0">
        <legend id={legendId} className="sr-only">
          {evaluatorName} configuration
        </legend>

        <div className="flex items-center justify-between gap-2">
          <p className="font-mono text-sm font-medium text-foreground">{evaluatorName}</p>
          <Toggle
            label="Enabled"
            checked={enabled}
            disabled={saving}
            onChange={(event) => edited(setEnabled)(event.target.checked)}
          />
        </div>

        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="flex flex-col gap-1 text-xs font-medium text-muted">
            Sampling rate (0–1)
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={samplingRate}
              disabled={saving}
              onChange={(event) => edited(setSamplingRate)(event.target.value)}
              className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-muted">
            Threshold (optional)
            <input
              type="number"
              step="any"
              placeholder="evaluator default"
              value={threshold}
              disabled={saving}
              onChange={(event) => edited(setThreshold)(event.target.value)}
              className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground placeholder:text-muted disabled:cursor-not-allowed disabled:opacity-50"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-muted">
            Max retries
            <input
              type="number"
              min={0}
              step={1}
              value={maxRetries}
              disabled={saving}
              onChange={(event) => edited(setMaxRetries)(event.target.value)}
              className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            />
          </label>
        </div>
      </fieldset>

      {status === "error" && error ? (
        <ErrorBanner title={titleForStatus(error.status)} message={error.message} />
      ) : null}

      <div className="flex items-center gap-2">
        <Button type="submit" variant="primary" disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
        {status === "saved" ? (
          <p role="status" className="text-sm text-status-ok">
            Saved
          </p>
        ) : null}
        {onCancel ? (
          <Button type="button" variant="ghost" onClick={onCancel} disabled={saving}>
            Cancel
          </Button>
        ) : null}
      </div>
    </form>
  );
}
