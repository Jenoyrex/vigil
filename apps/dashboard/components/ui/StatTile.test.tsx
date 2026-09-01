import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatTile } from "./StatTile";

describe("StatTile", () => {
  it("renders the label, value, and optional sublabel", () => {
    render(<StatTile label="Error Rate" value="2.34%" sublabel="42 errors" />);
    expect(screen.getByText("Error Rate")).toBeInTheDocument();
    expect(screen.getByText("2.34%")).toBeInTheDocument();
    expect(screen.getByText("42 errors")).toBeInTheDocument();
  });

  it("omits the sublabel element when not provided", () => {
    render(<StatTile label="Total Spans" value="1,234" />);
    expect(screen.queryByText(/errors/)).not.toBeInTheDocument();
  });

  it("applies error-tone styling to the value when tone='error'", () => {
    render(<StatTile label="Error Rate" value="10%" tone="error" />);
    expect(screen.getByText("10%")).toHaveClass("text-status-error");
  });
});
