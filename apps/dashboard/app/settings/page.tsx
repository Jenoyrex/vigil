import type { Metadata } from "next";
import Link from "next/link";

import { ApiKeysManager } from "@/components/settings/ApiKeysManager";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { VigilApiError } from "@/lib/api/types";
import { listApiKeys, requireProject } from "@/lib/api/workspace";
import type { ApiKeyOut } from "@/lib/api/workspaceTypes";

export const metadata: Metadata = {
  title: "Settings — Vigil",
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[8rem_1fr] gap-3 py-2 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-all text-foreground">{children}</dd>
    </div>
  );
}

export default async function SettingsPage() {
  const { me, currentOrganization, currentProject } = await requireProject();
  const canManage = currentOrganization?.role !== "member";

  let keys: ApiKeyOut[] = [];
  let keysError: string | null = null;
  try {
    keys = (await listApiKeys(currentProject.id)).items;
  } catch (error) {
    keysError = error instanceof VigilApiError ? error.message : "Unable to load API keys.";
  }

  return (
    <div className="max-w-4xl space-y-10">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold text-foreground">Settings</h1>
        <p className="text-sm text-muted">
          {currentOrganization?.name} / {currentProject.name}
        </p>
      </div>

      <section className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold text-foreground">API keys</h2>
          <Link href="/connect" className="text-sm text-accent hover:underline">
            Quickstart
          </Link>
        </div>
        <p className="text-sm text-muted">
          Applications authenticate to Vigil with a project API key (<code className="font-mono text-xs">Authorization: Bearer vgl_…</code>).
          Only a hash is stored, so a key is shown once when created. Revoking a key stops it immediately.
        </p>
        {keysError ? <ErrorBanner message={keysError} /> : <ApiKeysManager initialKeys={keys} canManage={canManage} />}
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-foreground">Project</h2>
        <dl className="divide-y divide-border rounded-lg border border-border px-4">
          <Row label="Name">{currentProject.name}</Row>
          <Row label="Project ID">
            <code className="font-mono text-xs">{currentProject.id}</code>
          </Row>
          <Row label="Organization">{currentOrganization?.name}</Row>
        </dl>
        {canManage ? (
          <Link href="/onboarding?new=project" className="inline-block text-sm text-accent hover:underline">
            Create another project
          </Link>
        ) : null}
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-foreground">Team</h2>
        <dl className="divide-y divide-border rounded-lg border border-border px-4">
          <Row label="You">{me.user.email}</Row>
          <Row label="Your role">{currentOrganization?.role}</Row>
        </dl>
        <p className="text-sm text-muted">
          Inviting teammates isn&apos;t available yet. Everyone who belongs to {currentOrganization?.name} can see its
          projects.
        </p>
      </section>
    </div>
  );
}
