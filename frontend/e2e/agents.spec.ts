import { expect, test } from "@playwright/test";

import { installBaseServer } from "./fixtures/server";

test("hydrates dynamic agents, queues, tasks, tools and checkpoints from bootstrap", async ({ page }) => {
  await installBaseServer(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Agent 运行态" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Agent 状态" }).getByText("researcher")).toBeVisible();
  await expect(page.getByText("Verify source")).toBeVisible();
  await expect(page.getByText("Review evidence")).toBeVisible();
  await expect(page.getByText("read_file")).toBeVisible();
  await expect(page.getByText("Evidence collected")).toBeVisible();
});
