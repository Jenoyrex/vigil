"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/Button";

const NAV_ITEMS = [
  { href: "/", label: "Overview" },
  { href: "/traces", label: "Traces" },
  { href: "/analytics", label: "Analytics" },
  { href: "/evaluations", label: "Evaluations" },
] as const;

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

export function TopNav() {
  const pathname = usePathname();

  if (pathname === "/login") return null;

  return (
    <header className="border-b border-border bg-surface">
      <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3 sm:px-6">
        <span className="font-mono text-sm font-semibold tracking-tight text-foreground">vigil</span>
        <nav aria-label="Primary" className="flex items-center gap-1">
          {NAV_ITEMS.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                  active
                    ? "bg-accent/10 text-accent"
                    : "text-muted hover:bg-surface-hover hover:text-foreground",
                )}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto">
          <LogoutButton />
        </div>
      </div>
    </header>
  );
}
