import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ExpandableSection } from "./ExpandableSection";

describe("ExpandableSection", () => {
  it("is collapsed by default -- content is not visible until expanded", () => {
    render(
      <ExpandableSection title="Input">
        <p>secret payload content</p>
      </ExpandableSection>,
    );
    const details = screen.getByText("Input").closest("details");
    expect(details).not.toHaveAttribute("open");
  });

  it("opens when the summary is clicked (native <details> keyboard/click toggling)", () => {
    render(
      <ExpandableSection title="Input">
        <p>payload content</p>
      </ExpandableSection>,
    );
    const summary = screen.getByText("Input").closest("summary");
    expect(summary).not.toBeNull();
    fireEvent.click(summary as HTMLElement);
    const details = screen.getByText("Input").closest("details");
    expect(details).toHaveAttribute("open");
  });

  it("respects defaultOpen", () => {
    render(
      <ExpandableSection title="Attributes" defaultOpen>
        <p>attribute content</p>
      </ExpandableSection>,
    );
    const details = screen.getByText("Attributes").closest("details");
    expect(details).toHaveAttribute("open");
  });

  it("shows a size hint and a truncation badge when provided", () => {
    render(
      <ExpandableSection title="Output" sizeHint="2.4 KB" truncated>
        <p>content</p>
      </ExpandableSection>,
    );
    expect(screen.getByText("2.4 KB")).toBeInTheDocument();
    expect(screen.getByText(/truncated/i)).toBeInTheDocument();
  });

  it("renders a copy button only when copyValue is provided", () => {
    const { rerender } = render(<ExpandableSection title="Input">content</ExpandableSection>);
    expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();

    rerender(
      <ExpandableSection title="Input" copyValue="the value">
        content
      </ExpandableSection>,
    );
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });
});
