import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { SignupForm } from "@/components/auth/SignupForm";
import { getWorkspace } from "@/lib/api/workspace";

export const metadata: Metadata = {
  title: "Create your account — Vigil",
};

export default async function SignupPage() {
  if (await getWorkspace()) redirect("/");

  return (
    <div className="flex flex-1 items-center justify-center py-12">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-1.5 text-center">
          <Link href="/" className="font-mono text-lg font-semibold text-foreground">
            vigil
          </Link>
          <h1 className="text-xl font-semibold tracking-tight text-foreground">Create your account</h1>
          <p className="text-sm text-muted">
            Then set up a project and send your first trace in a few minutes.
          </p>
        </div>
        {process.env.VIGIL_DEMO_MODE === "true" && (
          <p role="note" className="rounded-md border border-border px-3 py-2 text-sm text-muted">
            This is a public demo. Use a throwaway email and a password you don&apos;t use anywhere
            else — demo accounts use lighter password hashing and are deleted after 30 days of
            inactivity.
          </p>
        )}
        <SignupForm />
      </div>
    </div>
  );
}
