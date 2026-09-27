"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/Button";
import { postJson } from "@/lib/api/postJson";
import type { Organization } from "@/lib/api/workspaceTypes";

const NAV_ITEMS = [
  { href: "/", label: "Overview" },
  { href: "/traces", label: "Traces" },
  { href: "/analytics", label: "Analytics" },
  { href: "/evaluations", label: "Evaluations" },
] as const;

const NEW_PROJECT = "__new_project__";

export interface TopNavWorkspace {
  email: string;
  organizations: Organization[];
  currentProjectId: string | null;
}

/**
 * POST /api/auth/logout, then navigate to /login and refresh so every
 * Server Component re-reads the now-cleared session cookie.
 */
function LogoutButton() {
  const router = useRouter();
  const [loggingOut, setLoggingOut] = useState(false);

  async function handleLogout(): Promise<void> {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await fetch("/api/auth/logout", { method: "POST", cache: "no-store" });
    } finally {
      router.push("/login");
      router.refresh();
    }
  }

  return (
    <Button variant="ghost" onClick={() => void handleLogout()} disabled={loggingOut}>
      {loggingOut ? "Logging out…" : "Log out"}
    </Button>
  );
}

/** Which organization/project is being viewed, and switching between them. */
function ProjectSwitcher({ workspace }: { workspace: TopNavWorkspace }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const canCreate = workspace.organizations.some((org) => org.role !== "member");

  async function handleChange(value: string): Promise<void> {
    if (value === NEW_PROJECT) {
      router.push("/onboarding?new=project");
      return;
    }
    setPending(true);
    try {
      await postJson("/api/workspace/current-project", { projectId: value });
      router.refresh();
    } finally {
      setPending(false);
    }
  }

  return (
    <label className="flex min-w-0 items-center">
      <span className="sr-only">Current project</span>
      <select
        value={workspace.currentProjectId ?? ""}
        disabled={pending}
        onChange={(event) => void handleChange(event.target.value)}
        className="max-w-56 truncate rounded-md border border-border bg-background py-1 pl-2 pr-7 text-sm font-medium text-foreground hover:bg-surface-hover disabled:opacity-60"
      >
        {workspace.organizations.map((org) => (
          <optgroup key={org.id} label={org.name}>
            {org.projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </optgroup>
        ))}
        {canCreate ? <option value={NEW_PROJECT}>+ New project…</option> : null}
      </select>
    </label>
  );
}

const linkClass = (active: boolean) =>
  cn(
    "whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
    active ? "bg-accent/10 text-accent" : "text-muted hover:bg-surface-hover hover:text-foreground",
  );

export function TopNav({ workspace }: { workspace: TopNavWorkspace | null }) {
  const pathname = usePathname();

  if (pathname === "/login" || pathname === "/signup") return null;

  const logo = (
    <Link href="/" className="font-mono text-sm font-semibold tracking-tight text-foreground">
      vigil
    </Link>
  );

  if (!workspace) {
    return (
      <header className="border-b border-border">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-3 sm:px-6">
          {logo}
          <div className="ml-auto flex items-center gap-2">
            <Link href="/login" className={linkClass(false)}>
              Log in
            </Link>
            <Link
              href="/signup"
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-foreground hover:opacity-90"
            >
              Get started
            </Link>
          </div>
        </div>
      </header>
    );
  }

  const inProject = workspace.currentProjectId !== null && pathname !== "/onboarding";

  return (
    <header className="border-b border-border bg-surface">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5 sm:px-6">
        {logo}
        {inProject ? (
          <>
            <span className="text-border" aria-hidden>
              /
            </span>
            <ProjectSwitcher workspace={workspace} />
            <nav aria-label="Primary" className="-mx-1 flex max-w-full items-center gap-1 overflow-x-auto px-1">
              {NAV_ITEMS.map((item) => {
                const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
                return (
                  <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined} className={linkClass(active)}>
                    {item.label}
                  </Link>
                );
              })}
            </nav>
          </>
        ) : null}
        <div className="ml-auto flex items-center gap-1">
          {inProject ? (
            <>
              <Link href="/connect" aria-current={pathname === "/connect" ? "page" : undefined} className={linkClass(pathname === "/connect")}>
                Connect app
              </Link>
              <Link href="/settings" aria-current={pathname === "/settings" ? "page" : undefined} className={linkClass(pathname === "/settings")}>
                Settings
              </Link>
            </>
          ) : (
            <span className="px-2 text-xs text-muted">{workspace.email}</span>
          )}
          <LogoutButton />
        </div>
      </div>
    </header>
  );
}
