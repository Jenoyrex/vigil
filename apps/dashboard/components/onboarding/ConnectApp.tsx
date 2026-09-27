"use client";

import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { CopyButton } from "@/components/ui/CopyButton";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { cn } from "@/lib/cn";
import { formatRelativeTime } from "@/lib/format";
import { postJson } from "@/lib/api/postJson";
import type { ApiKeyCreated } from "@/lib/api/workspaceTypes";
import { useLatestTrace } from "@/lib/hooks/useLatestTrace";

import { CodeBlock } from "./CodeBlock";
import { StepIndicator } from "./StepIndicator";

const SDK_INSTALL = `pip install "git+https://github.com/Jenoyrex/vigil.git#subdirectory=packages/sdk-python"`;

const PYTHON_EXAMPLE = `from vigil import Vigil

# Reads VIGIL_API_KEY and VIGIL_BASE_URL from the environment.
vigil = Vigil(service_name="my-llm-app")

question = "What is the capital of France?"

with vigil.start_span("answer question", span_type="llm") as span:
    span.set_input(question)
    # Replace with your real model call, e.g. an OpenAI/Anthropic client:
    answer = "Paris is the capital of France."
    span.set_output(answer)
    span.record_llm_usage(provider="openai", model="gpt-4o-mini")

vigil.flush()   # send now instead of waiting for the background batch
vigil.close()`;

function envBlock(baseUrl: string, key: string): string {
  return `export VIGIL_API_KEY="${key}"\nexport VIGIL_BASE_URL="${baseUrl}"`;
}

function curlExample(baseUrl: string, key: string): string {
  return `curl -X POST "${baseUrl}/v1/traces" \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
  "resource": { "service.name": "my-llm-app" },
  "spans": [{
    "trace_id": "'$(openssl rand -hex 16)'",
    "span_id": "'$(openssl rand -hex 8)'",
    "name": "answer question",
    "span_type": "llm",
    "start_time": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'",
    "end_time": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'",
    "status": "ok",
    "input": "What is the capital of France?",
    "output": "Paris is the capital of France.",
    "llm_provider": "openai",
    "llm_model": "gpt-4o-mini"
  }]
}'`;
}

function mask(key: string): string {
  const [prefix] = key.split(".");
  return `${prefix}.${"•".repeat(20)}`;
}

function Section({
  number,
  title,
  done,
  children,
}: {
  number: number;
  title: string;
  done?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className="grid grid-cols-[1.75rem_1fr] gap-x-3 gap-y-3">
      <span
        className={cn(
          "flex h-7 w-7 items-center justify-center rounded-full font-mono text-xs font-semibold",
          done ? "bg-status-ok-bg text-status-ok" : "border border-border text-muted",
        )}
        aria-hidden
      >
        {done ? "✓" : number}
      </span>
      <h2 className="self-center text-sm font-semibold text-foreground">{title}</h2>
      <div className="col-start-2 min-w-0 space-y-3">{children}</div>
    </section>
  );
}

/**
 * "Connect your app": create a real API key (shown once), follow the real
 * Python SDK / HTTP quickstart, optionally fire a real test trace through
 * ingestion, and watch for the first trace to arrive.
 */
export function ConnectApp({
  projectName,
  apiBaseUrl,
  canManageKeys,
  onboarding,
}: {
  projectName: string;
  apiBaseUrl: string;
  canManageKeys: boolean;
  onboarding: boolean;
}) {
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [creating, setCreating] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [tab, setTab] = useState<"python" | "http">("python");
  const [sendingTest, setSendingTest] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [testSent, setTestSent] = useState(false);
  const trace = useLatestTrace();

  const key = created?.api_key ?? "vgl_your_key";
  const shownKey = created ? mask(created.api_key) : "vgl_your_key";

  async function createKey(): Promise<void> {
    if (creating) return;
    setCreating(true);
    setKeyError(null);
    try {
      setCreated(await postJson<ApiKeyCreated>("/api/workspace/api-keys", { name: "Quickstart key" }));
    } catch (caught) {
      setKeyError((caught as Error).message);
    } finally {
      setCreating(false);
    }
  }

  async function sendTestTrace(): Promise<void> {
    if (!created || sendingTest) return;
    setSendingTest(true);
    setTestError(null);
    try {
      await postJson("/api/workspace/test-trace", { apiKey: created.api_key });
      setTestSent(true);
    } catch (caught) {
      setTestError((caught as Error).message);
    } finally {
      setSendingTest(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl space-y-8 py-4">
      {onboarding ? <StepIndicator current="Connect your app" /> : null}
      <div className="space-y-1.5">
        <p className="font-mono text-xs text-muted">{projectName}</p>
        <h1 className="text-xl font-semibold tracking-tight text-foreground">Connect your application</h1>
        <p className="text-sm text-muted">
          Your application sends traces to Vigil with an API key. Vigil stores each LLM call&apos;s
          input, output, latency and model, runs your enabled evaluators on it, and shows the results
          here.
        </p>
      </div>

      <Section number={1} title="Create an API key" done={created !== null}>
        {created ? (
          <div className="space-y-2 rounded-md border border-accent/40 bg-accent/5 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <code className="break-all font-mono text-sm text-foreground">{created.api_key}</code>
              <CopyButton value={created.api_key} label="Copy key" />
            </div>
            <p className="text-xs text-muted">
              <strong className="font-medium text-foreground">Copy it now — this is the only time it&apos;s shown.</strong>{" "}
              Keep it secret: use it only in server-side code, never commit it to source control or ship it
              in browser/mobile code. You can revoke it in{" "}
              <Link href="/settings" className="text-accent hover:underline">
                Settings
              </Link>
              .
            </p>
          </div>
        ) : canManageKeys ? (
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" onClick={() => void createKey()} disabled={creating}>
              {creating ? "Creating…" : "Create API key"}
            </Button>
            <span className="text-xs text-muted">Keys belong to this project only.</span>
          </div>
        ) : (
          <p className="text-sm text-muted">Ask an organization owner or admin for an API key for this project.</p>
        )}
        {keyError ? <ErrorBanner title="Couldn't create a key" message={keyError} /> : null}
      </Section>

      <Section number={2} title="Install and configure">
        <div role="tablist" aria-label="Integration" className="flex gap-1 border-b border-border">
          {(
            [
              ["python", "Python SDK"],
              ["http", "HTTP API"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              role="tab"
              type="button"
              aria-selected={tab === value}
              onClick={() => setTab(value)}
              className={cn(
                "-mb-px border-b-2 px-3 py-1.5 text-sm font-medium transition-colors",
                tab === value ? "border-accent text-foreground" : "border-transparent text-muted hover:text-foreground",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        {tab === "python" ? (
          <>
            <p className="text-xs text-muted">Python 3.12+. The SDK is installed straight from the Vigil repository.</p>
            <CodeBlock label="shell" code={SDK_INSTALL} />
            <CodeBlock label="shell · environment" code={envBlock(apiBaseUrl, shownKey)} copyValue={envBlock(apiBaseUrl, key)} />
          </>
        ) : (
          <p className="text-xs text-muted">
            No SDK for your language? Send spans directly to <code className="font-mono">POST {apiBaseUrl}/v1/traces</code> with
            your key as a Bearer token. A TypeScript SDK doesn&apos;t exist yet.
          </p>
        )}
      </Section>

      <Section number={3} title="Send your first trace" done={trace !== null}>
        {tab === "python" ? (
          <CodeBlock label="first_trace.py" code={PYTHON_EXAMPLE} />
        ) : (
          <CodeBlock label="shell" code={curlExample(apiBaseUrl, shownKey)} copyValue={curlExample(apiBaseUrl, key)} />
        )}
        <p className="text-xs text-muted">
          Wrap each model call in a <code className="font-mono">span_type=&quot;llm&quot;</code> span with its input and
          output — that&apos;s what the relevance evaluator scores. Nested spans (retrieval, tools) become one trace.
        </p>
        {created ? (
          <div className="flex flex-wrap items-center gap-3 rounded-md border border-dashed border-border p-3 text-sm">
            <Button onClick={() => void sendTestTrace()} disabled={sendingTest || testSent}>
              {sendingTest ? "Sending…" : testSent ? "Test trace sent" : "Send a test trace"}
            </Button>
            <span className="text-xs text-muted">
              Just checking the pipeline? This sends one real, clearly labeled test span through the ingestion API with
              your new key.
            </span>
          </div>
        ) : null}
        {testError ? <ErrorBanner title="Test trace failed" message={testError} /> : null}
      </Section>

      <div
        role="status"
        className={cn(
          "rounded-lg border p-4",
          trace ? "border-status-ok/40 bg-status-ok-bg/40" : "border-border bg-surface",
        )}
      >
        {trace ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-foreground">Trace received</p>
              <p className="text-xs text-muted">
                {trace.root_span_name ?? trace.trace_id} · {formatRelativeTime(trace.start_time)}
              </p>
            </div>
            <div className="flex gap-2">
              <Link
                href={`/traces/${trace.trace_id}?start=${encodeURIComponent(trace.start_time)}`}
                className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-foreground hover:opacity-90"
              >
                View trace
              </Link>
              <Link
                href="/"
                className="rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-hover"
              >
                Go to dashboard
              </Link>
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-3 text-sm">
            <span className="relative flex h-2.5 w-2.5" aria-hidden>
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60 motion-reduce:animate-none" />
              <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-accent" />
            </span>
            <span className="text-foreground">Waiting for your first trace…</span>
            <span className="text-xs text-muted">This updates automatically.</span>
          </div>
        )}
      </div>
    </div>
  );
}
