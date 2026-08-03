import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { ServerConnectionForm } from "./ServerConnectionForm";

it("never hydrates a saved deployment token into the DOM and can clear it explicitly", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn();
  render(<ServerConnectionForm
    initialValue={{ serverUrl: "https://agents.example", token: "deployment-secret" }}
    onSave={onSave}
  />);

  expect(screen.getByLabelText("访问令牌")).toHaveValue("");
  expect(screen.getByText("访问令牌已配置")).toBeVisible();
  expect(document.body.textContent).not.toContain("deployment-secret");

  await user.click(screen.getByRole("button", { name: "保存并重新连接" }));
  expect(onSave).toHaveBeenLastCalledWith({
    serverUrl: "https://agents.example",
    token: "deployment-secret",
  });
  await user.click(screen.getByRole("button", { name: "清除访问令牌" }));
  expect(onSave).toHaveBeenLastCalledWith({
    serverUrl: "https://agents.example",
    token: "",
  });
});
