import { expect, test } from "@playwright/test";

import { bootstrap, installBaseServer, json } from "./fixtures/server";

test("recovers from a transient bootstrap failure without losing the workspace", async ({ page }) => {
  let bootstraps = 0;
  await installBaseServer(page, async (route, path) => {
    if (path !== "/api/v1/bootstrap") return false;
    bootstraps += 1;
    if (bootstraps === 1) {
      await json(route, {
        error: { code: "RUNTIME_UNAVAILABLE", details: {}, message: "Runtime unavailable", retryable: true },
        requestId: "recovery-1",
      }, 503);
    } else await json(route, bootstrap);
    return true;
  });

  await page.goto("/");
  await expect(page.getByRole("status")).toContainText("连接已中断");
  await page.reload();
  await expect(page.getByText("Manyselves")).toBeVisible();
  await expect(page.locator(".connection-banner")).not.toContainText("连接已中断");
  expect(bootstraps).toBeGreaterThanOrEqual(2);
});
