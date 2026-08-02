import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CommandPalette } from "./CommandPalette";

describe("CommandPalette", () => {
  it("offers only commands supported by the current HTTP contract", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<CommandPalette onSelect={onSelect} query="/" />);

    expect(screen.getByRole("button", { name: /\/history/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /\/stop/ })).toBeVisible();
    expect(screen.queryByText(/\/compact/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /\/history/ }));
    expect(onSelect).toHaveBeenCalledWith("/history");
  });
});
