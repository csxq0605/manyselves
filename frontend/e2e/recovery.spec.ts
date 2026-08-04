import { expect, test } from "@playwright/test";

import { bootstrap, installBaseServer, json } from "./fixtures/server";

test("refreshes bootstrap after an explicit stream resync signal", async ({ page }) => {
  let bootstrapRequests = 0;
  await installBaseServer(page, async (route, path) => {
    if (path === "/api/v1/auth/session") {
      await json(route, { authenticated: true, expiresAt: "2030-01-01T00:00:00Z", username: "admin" });
      return true;
    }
    if (path === "/api/v1/bootstrap") {
      bootstrapRequests += 1;
      await json(route, bootstrap);
      return true;
    }
    if (path === "/api/v1/events") {
      if (bootstrapRequests < 2) {
        const event = {
          eventId: "stream-1:evt-1", payload: {}, schemaVersion: 1, sequence: 1,
          streamId: "stream-1", timestamp: "2026-08-03T08:00:00Z", type: "stream.resync_required",
        };
        await route.fulfill({ body: `data: ${JSON.stringify(event)}\n\n`, contentType: "text/event-stream" });
      } else {
        await route.fulfill({ body: "", contentType: "text/event-stream" });
      }
      return true;
    }
    return false;
  });

  await page.goto("/");
  await expect.poll(() => bootstrapRequests).toBeGreaterThan(1);
  await page.getByRole("link", { name: "运行态" }).click();
  await expect(page.getByRole("heading", { name: "运行态" })).toBeVisible();
});
