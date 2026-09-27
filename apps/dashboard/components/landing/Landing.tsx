import Link from "next/link";

const SNIPPET = `from vigil import Vigil

vigil = Vigil(service_name="support-bot")

with vigil.start_span("answer", span_type="llm") as span:
    span.set_input(question)
    answer = llm.complete(question)
    span.set_output(answer)
    span.record_llm_usage(provider="openai", model="gpt-4o-mini")`;

const QUESTIONS = [
  ["What is my application sending to the model?", "Every prompt and response, with the trace of retrieval and tool calls around it."],
  ["Which responses missed the point?", "Each LLM response gets a relevance score and a pass/fail label against its input."],
  ["Where does the time go?", "Per-span latency in a waterfall, plus p50/p95 latency and error rate over time."],
  ["Is quality drifting?", "Evaluation results and token usage by model, environment and release."],
] as const;

const STEPS = [
  ["Instrument", "Wrap model calls in spans with the Python SDK, or POST spans to the HTTP API from any language."],
  ["Capture", "Vigil stores each trace — input, output, model, tokens, latency, errors — scoped to your project."],
  ["Evaluate", "Enabled evaluators score LLM responses in the background on Vigil's own workers. Prompts never go to a third-party model."],
] as const;

export function Landing() {
  return (
    <div className="mx-auto max-w-5xl space-y-20 py-10 sm:py-16">
      <section className="grid items-center gap-10 lg:grid-cols-[1.1fr_1fr]">
        <div className="space-y-6">
          <p className="font-mono text-xs uppercase tracking-widest text-accent">LLM observability · evaluation</p>
          <h1 className="text-4xl font-semibold leading-[1.1] tracking-tight text-foreground sm:text-5xl">
            See what your LLM application is actually doing.
          </h1>
          <p className="max-w-xl text-base leading-relaxed text-muted">
            Vigil records every model call your application makes — prompt, response, latency, model and tokens — and
            scores each response for relevance, so you know which answers need a closer look.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Link
              href="/signup"
              className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-foreground hover:opacity-90"
            >
              Get started
            </Link>
            <Link
              href="/login"
              className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground hover:bg-surface-hover"
            >
              Log in
            </Link>
          </div>
        </div>
        <div className="overflow-hidden rounded-lg border border-border bg-surface shadow-sm">
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <span className="font-mono text-xs text-muted">app.py</span>
          </div>
          <pre className="overflow-x-auto px-4 py-3 font-mono text-[12.5px] leading-relaxed text-foreground">
            <code>{SNIPPET}</code>
          </pre>
        </div>
      </section>

      <section className="space-y-6">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-muted">Questions Vigil answers</h2>
        <dl className="grid gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-2">
          {QUESTIONS.map(([question, answer]) => (
            <div key={question} className="space-y-1.5 bg-background p-5">
              <dt className="text-sm font-medium text-foreground">{question}</dt>
              <dd className="text-sm text-muted">{answer}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="space-y-6">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-muted">How it works</h2>
        <ol className="grid gap-6 sm:grid-cols-3">
          {STEPS.map(([title, body], index) => (
            <li key={title} className="space-y-2">
              <span className="font-mono text-xs text-accent">0{index + 1}</span>
              <p className="text-sm font-medium text-foreground">{title}</p>
              <p className="text-sm leading-relaxed text-muted">{body}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="flex flex-col gap-4 rounded-lg border border-border bg-surface p-6 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <p className="text-sm font-medium text-foreground">From sign-up to your first evaluated trace in a few minutes.</p>
          <p className="text-sm text-muted">
            Today: Python SDK and HTTP API, relevance evaluation. Groundedness and faithfulness evaluators are planned,
            not shipped.
          </p>
        </div>
        <Link
          href="/signup"
          className="shrink-0 rounded-md bg-accent px-4 py-2 text-center text-sm font-medium text-accent-foreground hover:opacity-90"
        >
          Create an account
        </Link>
      </section>
    </div>
  );
}
