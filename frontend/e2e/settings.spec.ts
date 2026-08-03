import { expect, test } from "@playwright/test";

import { installBaseServer, json } from "./fixtures/server";

const settings = {
  defaults: { maxTokens: 4096, model: "gpt-4.1", provider: "openai", temperature: 0.2 },
  providers: [{
    active: true, apiBase: "https://api.openai.com/v1", configured: true,
    defaultModel: "gpt-4.1", enabled: true, id: "openai-primary",
    name: "OpenAI Primary", provider: "openai",
  }],
};

test("loads masked server settings and never renders provider credentials", async ({ page }) => {
  await installBaseServer(page, async (route, path) => {
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
  await page.getByRole("button", { name: "设置" }).click();
  await expect(page.getByRole("heading", { name: "Provider 管理" })).toBeVisible();
  await expect(page.getByText("凭据已配置")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("sk-");
});
