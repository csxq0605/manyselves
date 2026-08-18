import { expect, test } from "@playwright/test";

import { installBaseServer } from "./fixtures/server";

test("hydrates dynamic agents, queues, tasks and tools from bootstrap", async ({ page }) => {
  await installBaseServer(page);
  await page.goto("/projects/project-1/runtime");

  await expect(page.getByRole("heading", { name: "运行态" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Agent 状态" }).getByText("researcher")).toBeVisible();
  await expect(page.getByText("Verify source")).toBeVisible();
  await expect(page.getByText("Review evidence")).toBeVisible();
  await expect(page.getByText("read_file")).toBeVisible();
});
