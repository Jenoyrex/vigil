"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/Button";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { TextField } from "@/components/ui/TextField";
import { postJson } from "@/lib/api/postJson";

export const MIN_PASSWORD_LENGTH = 12;

/** Client-side checks mirror apps/api's SignupRequest, which remains the authority. */
export function validateSignup(password: string, confirm: string): string | null {
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Use at least ${MIN_PASSWORD_LENGTH} characters for your password.`;
  }
  if (password !== confirm) return "Passwords don't match.";
  return null;
}

export function SignupForm() {
  const router = useRouter();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submitting) return;
    const invalid = validateSignup(password, confirm);
    if (invalid) {
      setError(invalid);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await postJson("/api/auth/signup", { email, password, fullName });
    } catch (caught) {
      setError((caught as Error).message);
      setSubmitting(false);
      return;
    }
    router.push("/onboarding");
    router.refresh();
  }

  return (
    <form onSubmit={(event) => void handleSubmit(event)} className="space-y-4" noValidate={false}>
      <TextField
        label="Name"
        autoComplete="name"
        value={fullName}
        disabled={submitting}
        onChange={(event) => setFullName(event.target.value)}
        placeholder="Optional"
        maxLength={200}
      />
      <TextField
        label="Work email"
        type="email"
        required
        autoComplete="email"
        value={email}
        disabled={submitting}
        onChange={(event) => setEmail(event.target.value)}
      />
      <TextField
        label="Password"
        type="password"
        required
        minLength={MIN_PASSWORD_LENGTH}
        maxLength={200}
        autoComplete="new-password"
        value={password}
        disabled={submitting}
        onChange={(event) => setPassword(event.target.value)}
        hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}
      />
      <TextField
        label="Confirm password"
        type="password"
        required
        autoComplete="new-password"
        value={confirm}
        disabled={submitting}
        onChange={(event) => setConfirm(event.target.value)}
      />
      {error ? <ErrorBanner title="Couldn't create your account" message={error} /> : null}
      <Button type="submit" variant="primary" className="w-full py-2" disabled={submitting}>
        {submitting ? "Creating account…" : "Create account"}
      </Button>
      <p className="text-center text-sm text-muted">
        Already have an account?{" "}
        <Link href="/login" className="font-medium text-accent hover:underline">
          Log in
        </Link>
      </p>
    </form>
  );
}
