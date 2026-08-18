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
  await expect(page.getByRole("alert")).toContainText("Unable to load the application.");
  await page.reload();
  await expect(page.getByRole("link", { name: "新对话" })).toBeVisible();
  expect(bootstraps).toBeGreaterThanOrEqual(2);
});
