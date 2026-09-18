"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/Button";
import { ErrorBanner } from "@/components/ui/ErrorBanner";

const GENERIC_ERROR = "Invalid email or password.";

/**
 * The dashboard login form (Phase 4D, F1). Posts to this app's own
 * `POST /api/auth/login` (never apps/api directly -- see that route's
 * docstring), which sets the session as an HttpOnly cookie; this component
 * never sees or stores the raw session token itself.
 *
 * `nextPath` is pre-sanitized by app/login/page.tsx (must start with a
 * single `/`, never `//...`) before it ever reaches this component, so a
 * successful login can never be turned into an open redirect via a
 * crafted `?next=` value.
 */
export function LoginForm({ nextPath }: { nextPath: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<"idle" | "submitting" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (status === "submitting") return;
    setStatus("submitting");
    setError(null);

    let response: Response;
    try {
      response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
        cache: "no-store",
      });
    } catch {
      setStatus("error");
      setError("Unable to reach the server. Please retry.");
      return;
    }

    if (!response.ok) {
      let detail = GENERIC_ERROR;
      try {
        const body = (await response.json()) as { detail?: string };
        if (typeof body.detail === "string" && body.detail) detail = body.detail;
      } catch {
        // Non-JSON error body -- fall back to the generic message above.
      }
      setStatus("error");
      setError(detail);
      return;
    }

    router.push(nextPath);
    router.refresh();
  }

  return (
    <form onSubmit={(event) => void handleSubmit(event)} className="w-full max-w-sm space-y-4">
      <label className="flex flex-col gap-1 text-sm font-medium text-muted">
        Email
        <input
          type="email"
          required
          autoComplete="username"
          value={email}
          disabled={status === "submitting"}
          onChange={(event) => setEmail(event.target.value)}
          className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm font-medium text-muted">
        Password
        <input
          type="password"
          required
          autoComplete="current-password"
          value={password}
          disabled={status === "submitting"}
          onChange={(event) => setPassword(event.target.value)}
          className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50"
        />
      </label>
      {status === "error" && error ? <ErrorBanner message={error} /> : null}
      <Button
        type="submit"
        variant="primary"
        className="w-full"
        disabled={status === "submitting"}
      >
        {status === "submitting" ? "Logging in…" : "Log in"}
      </Button>
    </form>
  );
}
