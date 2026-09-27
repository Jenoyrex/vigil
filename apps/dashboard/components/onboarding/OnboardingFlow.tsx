"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/Button";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { TextField } from "@/components/ui/TextField";
import { postJson } from "@/lib/api/postJson";
import type { Organization } from "@/lib/api/workspaceTypes";

import { StepIndicator } from "./StepIndicator";

interface OrgRef {
  id: string;
  name: string;
}

/**
 * Organization -> project, each a real write to apps/api (via
 * /api/workspace/**). Starts at the project step when the user already
 * has an organization (e.g. adding a second project).
 */
export function OnboardingFlow({ organization }: { organization: OrgRef | null }) {
  const router = useRouter();
  const [org, setOrg] = useState<OrgRef | null>(organization);
  const [name, setName] = useState("");
  const [enableRelevance, setEnableRelevance] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const step = org ? "Project" : "Organization";

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submitting || !name.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      if (!org) {
        const created = await postJson<Organization>("/api/workspace/organizations", { name });
        setOrg({ id: created.id, name: created.name });
        setName("");
        setSubmitting(false);
        return;
      }
      await postJson("/api/workspace/projects", { organizationId: org.id, name, enableRelevance });
    } catch (caught) {
      setError((caught as Error).message);
      setSubmitting(false);
      return;
    }
    router.push("/connect");
    router.refresh();
  }

  return (
    <div className="mx-auto w-full max-w-lg space-y-8 py-8">
      <StepIndicator current={step} />
      <form onSubmit={(event) => void handleSubmit(event)} className="space-y-5">
        {org ? (
          <div className="space-y-1.5">
            <p className="font-mono text-xs text-muted">{org.name}</p>
            <h1 className="text-xl font-semibold tracking-tight text-foreground">Create a project</h1>
            <p className="text-sm text-muted">
              A project holds the traces, API keys and evaluations for one AI application — for
              example your support chatbot or a RAG service. Its data is visible only to members of
              your organization.
            </p>
          </div>
        ) : (
          <div className="space-y-1.5">
            <h1 className="text-xl font-semibold tracking-tight text-foreground">Create your organization</h1>
            <p className="text-sm text-muted">
              Organizations own projects. You&apos;ll be its owner; usually this is your company or team
              name.
            </p>
          </div>
        )}

        <TextField
          key={step}
          label={org ? "Project name" : "Organization name"}
          placeholder={org ? "Support chatbot" : "Acme Inc."}
          required
          maxLength={200}
          autoFocus
          value={name}
          disabled={submitting}
          onChange={(event) => setName(event.target.value)}
        />

        {org ? (
          <label className="flex items-start gap-3 rounded-md border border-border bg-surface p-3 text-sm">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
              checked={enableRelevance}
              disabled={submitting}
              onChange={(event) => setEnableRelevance(event.target.checked)}
            />
            <span className="space-y-0.5">
              <span className="block font-medium text-foreground">Evaluate responses for relevance</span>
              <span className="block text-muted">
                Scores how relevant each LLM response is to its input, for every <code className="font-mono text-xs">llm</code> span.
                Runs on Vigil&apos;s own workers — your prompts are never sent to a third-party model.
                You can change this later under Evaluations.
              </span>
            </span>
          </label>
        ) : null}

        {error ? <ErrorBanner title="That didn't work" message={error} /> : null}

        <Button type="submit" variant="primary" className="py-2" disabled={submitting || !name.trim()}>
          {submitting ? "Saving…" : org ? "Create project" : "Continue"}
        </Button>
      </form>
    </div>
  );
}
