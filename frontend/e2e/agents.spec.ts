import { expect, test } from "@playwright/test";

import { installBaseServer } from "./fixtures/server";

test("hydrates dynamic agents and tools from bootstrap", async ({ page }) => {
  await installBaseServer(page);
  await page.goto("/projects/project-1/runtime");

  await expect(page.getByRole("heading", { name: "运行态" })).toBeVisible();
  // Agents and tool calls sit in the folded details since the page shows a Run as stages.
  await page.getByText("内部步骤与运行明细").click();
  await expect(page.getByRole("region", { name: "Agent 状态" }).getByText("researcher")).toBeVisible();
  await expect(page.getByRole("region", { name: "工具调用（最新在上）" }).getByText("read_file")).toBeVisible();
});
