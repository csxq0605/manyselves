import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SaveConflictDialog } from "./SaveConflictDialog";

describe("SaveConflictDialog", () => {
  it("offers reload, keep-draft, and explicit overwrite actions", async () => {
    const actions: string[] = [];
    const user = userEvent.setup();
    const { rerender } = render(
      <SaveConflictDialog
        onKeepDraft={() => actions.push("keep")}
        onOverwrite={() => actions.push("overwrite")}
        onReload={() => actions.push("reload")}
        path="Inputs/a.md"
      />,
    );

    expect(screen.getByRole("dialog")).toHaveTextContent("服务器文件已更新");
    await user.click(screen.getByRole("button", { name: "保留草稿" }));
    rerender(
      <SaveConflictDialog
        onKeepDraft={() => actions.push("keep")}
        onOverwrite={() => actions.push("overwrite")}
        onReload={() => actions.push("reload")}
        path="Inputs/a.md"
      />,
    );
    await user.click(screen.getByRole("button", { name: "重新加载服务器版本" }));
    await user.click(screen.getByRole("button", { name: "覆盖服务器版本" }));

    expect(actions).toEqual(["keep", "reload", "overwrite"]);
  });
});
