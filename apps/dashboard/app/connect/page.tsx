import type { Metadata } from "next";

import { ConnectApp } from "@/components/onboarding/ConnectApp";
import { hasRecentTrace } from "@/lib/api/traces";
import { publicApiBaseUrl, requireProject } from "@/lib/api/workspace";

export const metadata: Metadata = {
  title: "Connect your app — Vigil",
};

export default async function ConnectPage() {
  const { currentProject, currentOrganization } = await requireProject();
  // First-run framing (the step indicator) only until the project has data.
  const onboarding = !(await hasRecentTrace().catch(() => false));

  return (
    <ConnectApp
      projectName={`${currentOrganization?.name ?? ""} / ${currentProject.name}`}
      apiBaseUrl={publicApiBaseUrl()}
      canManageKeys={currentOrganization?.role !== "member"}
      onboarding={onboarding}
    />
  );
}
