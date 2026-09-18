import type { Metadata } from "next";

import { LoginForm } from "@/components/auth/LoginForm";

export const metadata: Metadata = {
  title: "Log in — Vigil",
};

/**
 * Only ever a same-origin, path-and-query reference -- never a full URL,
 * never `//host/path` (a protocol-relative URL browsers treat as
 * cross-origin), and never containing a backslash (some URL parsers treat
 * a leading `/\` the same as `//`, per the WHATWG URL spec's backslash
 * handling for special schemes -- a known bypass class for exactly this
 * kind of check, even though this app's own `next` values, built in
 * proxy.ts from `pathname + search`, never contain one) -- so `?next=` can
 * never be turned into an open redirect after login. See
 * components/auth/LoginForm.tsx's docstring for where this value is used.
 */
export function sanitizeNextPath(value: string | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return "/";
  }
  return value;
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const resolved = await searchParams;
  const nextParam = resolved.next;
  const nextPath = sanitizeNextPath(typeof nextParam === "string" ? nextParam : undefined);

  return (
    <div className="flex flex-1 items-center justify-center py-12">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-1 text-center">
          <p className="font-mono text-lg font-semibold text-foreground">vigil</p>
          <p className="text-sm text-muted">Log in to continue</p>
        </div>
        <LoginForm nextPath={nextPath} />
      </div>
    </div>
  );
}
