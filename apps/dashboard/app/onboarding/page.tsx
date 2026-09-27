import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { OnboardingFlow } from "@/components/onboarding/OnboardingFlow";
import { getWorkspace } from "@/lib/api/workspace";

export const metadata: Metadata = {
  title: "Set up Vigil",
};

/**
 * First-run setup (and "New project" later). A user with no organization
 * starts at the organization step; one with an organization but no project
 * -- or who asked for a new project -- starts at the project step, in the
 * current organization. Anyone already set up goes to the dashboard.
 */
export default async function OnboardingPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const workspace = await getWorkspace();
  if (!workspace) redirect("/login?next=/onboarding");

  const wantsNewProject = (await searchParams).new === "project";
  const org = workspace.currentOrganization;
  if (org && org.projects.length > 0 && !wantsNewProject) redirect("/");
  if (org && org.role === "member") {
    return (
      <div className="mx-auto max-w-lg py-16 text-sm text-muted">
        <h1 className="mb-2 text-lg font-semibold text-foreground">Ask an owner to create a project</h1>
        You&apos;re a member of {org.name}. Only its owners and admins can create projects.
      </div>
    );
  }

  return <OnboardingFlow organization={org ? { id: org.id, name: org.name } : null} />;
}
