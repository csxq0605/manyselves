import { expect, test, type Route } from "@playwright/test";

import { installBaseServer, json } from "./fixtures/server";

async function installLoginServer(page: Parameters<typeof installBaseServer>[0]) {
  let authenticated = false;
  await installBaseServer(page, async (route: Route, path: string) => {
    const request = route.request();
    if (path === "/api/v1/auth/session") {
      if (!authenticated) {
        await json(route, {
          error: { code: "UNAUTHORIZED", details: {}, message: "login required", retryable: false },
          requestId: "auth-required",
        }, 401);
        return true;
      }
      await json(route, { authenticated: true, expiresAt: "2030-01-01T00:00:00Z", username: "admin" });
      return true;
    }
    if (path === "/api/v1/auth/login") {
      expect(request.method()).toBe("POST");
      expect(request.postDataJSON()).toEqual({ username: "admin", password: "yuanxi@2026" });
      authenticated = true;
      await route.fulfill({ status: 204 });
      return true;
    }
    if (path === "/api/v1/control/lease") {
      await json(route, {
        actorId: "admin",
        clientId: "browser",
        expiresAt: "2030-01-01T00:01:00Z",
        leaseToken: "lease-1",
      }, 201);
      return true;
    }
    return false;
  });
}

test("opens on the login page and authenticates with the default admin account", async ({ page }) => {
  await installLoginServer(page);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "登录 manyselves" })).toBeVisible();
  await expect(page.getByLabel("用户名")).toHaveValue("admin");

  await page.getByLabel("密码").fill("yuanxi@2026");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByRole("link", { name: "新对话" })).toBeVisible();
  await expect(page.getByRole("link", { name: "全局知识库" })).toBeVisible();
  await expect(page.getByText("admin")).toBeVisible();
});
