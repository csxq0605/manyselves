import { expect, test } from "@playwright/test";

import { installBaseServer, json } from "./fixtures/server";

test("loads authoritative conversations and sends a named message command", async ({ page }) => {
  let sentBody: unknown = null;
  await installBaseServer(page, async (route, path) => {
    if (path === "/api/v1/agents/main/messages") {
      sentBody = route.request().postDataJSON();
      await json(route, { commandId: "message-command", status: "accepted" }, 202);
      return true;
    }
    return false;
  });

  await page.goto("/");
  await expect(page.getByRole("button", { name: /需求梳理/ })).toHaveAttribute("aria-current", "true");
  await page.getByRole("textbox", { name: "消息" }).fill("检查能源基线");
  await page.getByRole("button", { name: "发送" }).click();
  await expect.poll(() => sentBody).toMatchObject({ content: "检查能源基线", source: "user" });
});

test("reports an unauthorized bootstrap without rendering a false online state", async ({ page }) => {
  await page.route("**/api/v1/bootstrap", (route) => json(route, {
    error: { code: "UNAUTHORIZED", details: {}, message: "token required", retryable: false },
    requestId: "request-401",
  }, 401));
  await page.goto("/");
  await expect(page.getByRole("status")).toHaveText("访问令牌无效，请重新连接");
});
