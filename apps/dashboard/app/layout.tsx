import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import type { ReactNode } from "react";

import { TopNav } from "@/components/layout/TopNav";
import { getWorkspace } from "@/lib/api/workspace";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Vigil — LLM observability and evaluation",
  description:
    "Trace every LLM call in your application, evaluate response relevance automatically, and see quality over time.",
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // null when signed out (or the session expired) -> public header.
  const workspace = await getWorkspace().catch(() => null);

  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col bg-background text-foreground">
        <TopNav
          workspace={
            workspace
              ? {
                  email: workspace.me.user.email,
                  organizations: workspace.me.organizations,
                  currentProjectId: workspace.currentProject?.id ?? null,
                }
              : null
          }
        />
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
