import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FileReferencePicker } from "./FileReferencePicker";

describe("FileReferencePicker", () => {
  it("selects the first file when the server list arrives after mount", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    const view = render(<FileReferencePicker availableFiles={[]} onChange={onChange} sessionId="s1" />);
    expect(screen.getByRole("button", { name: "添加服务器文件" })).toBeDisabled();

    view.rerender(<FileReferencePicker
      availableFiles={["Inputs/late.md"]}
      onChange={onChange}
      sessionId="s1"
    />);
    await user.click(screen.getByRole("button", { name: "添加服务器文件" }));

    expect(onChange).toHaveBeenLastCalledWith([{ file: "Inputs/late.md", type: "file" }]);
  });
});
