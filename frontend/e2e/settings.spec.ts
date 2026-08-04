import { expect, test } from "@playwright/test";

import { installBaseServer, json } from "./fixtures/server";

const settings = {
  defaults: { maxTokens: 4096, model: "gpt-4.1", provider: "openai", temperature: 0.2 },
  providers: [{
    active: true, apiBase: "https://api.openai.com/v1", configured: true,
    credentialSource: "yaml",
    defaultModel: "gpt-4.1", enabled: true, id: "openai-primary",
    name: "OpenAI Primary", provider: "openai",
  }],
};

test("loads masked server settings and never renders provider credentials", async ({ page }) => {
  await installBaseServer(page, async (route, path) => {
    if (path === "/api/v1/auth/session") {
      await json(route, { authenticated: true, expiresAt: "2030-01-01T00:00:00Z", username: "admin" });
      return true;
    }
    if (path === "/api/v1/settings") { await json(route, settings); return true; }
    if (path === "/api/v1/settings/presets") { await json(route, { presets: [] }); return true; }
    if (path === "/api/v1/settings/validate") {
      await json(route, { availableProviders: ["openai"], errors: [], valid: true }); return true;
    }
    if (path === "/api/v1/agents") {
      await json(route, { agents: [{ id: "main", sessionId: "session-1", status: "idle" }] }); return true;
    }
    if (path === "/api/v1/agents/main/debug") {
      await json(route, { agentId: "main", enabled: false, entries: [] }); return true;
    }
    return false;
  });

  await page.goto("/");
  await page.getByRole("button", { name: "账户与设置" }).click();
  await page.getByRole("link", { name: "模型设置" }).click();
  await expect(page.getByRole("heading", { name: "模型配置" })).toBeVisible();
  await expect(page.getByRole("link", { name: "模型设置" })).not.toBeVisible();
  await expect(page.getByLabel("API Key")).toHaveValue("");
  await expect(page.getByText("高级设置")).toBeVisible();
  await expect(page.getByText("提供商管理", { exact: true })).not.toBeVisible();
  await expect(page.locator("body")).not.toContainText("sk-");
});
