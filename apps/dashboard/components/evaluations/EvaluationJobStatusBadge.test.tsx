import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EvaluationJobStatus } from "@/lib/api/types";

import { EvaluationJobStatusBadge } from "./EvaluationJobStatusBadge";

describe("EvaluationJobStatusBadge", () => {
  it.each<[EvaluationJobStatus, string]>([
    ["pending", "text-muted"],
    ["running", "text-accent"],
    ["succeeded", "text-status-ok"],
    ["failed", "text-status-error"],
    ["dead_letter", "text-status-error"],
  ])("renders %s with the expected tone class", (status, expectedClass) => {
    render(<EvaluationJobStatusBadge status={status} />);
    expect(screen.getByText(status)).toHaveClass(expectedClass);
  });

  it("renders every one of the five API statuses without throwing", () => {
    const statuses: EvaluationJobStatus[] = ["pending", "running", "succeeded", "failed", "dead_letter"];
    for (const status of statuses) {
      render(<EvaluationJobStatusBadge status={status} />);
      expect(screen.getByText(status)).toBeInTheDocument();
    }
  });
});
