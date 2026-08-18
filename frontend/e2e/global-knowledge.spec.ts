import { expect, test } from "@playwright/test";

import { installBaseServer, json } from "./fixtures/server";

const revision = "c".repeat(64);

async function installKnowledgeServer(page: Parameters<typeof installBaseServer>[0]) {
  await installBaseServer(page, async (route, path) => {
    const request = route.request();
    const url = new URL(request.url());
    if (path === "/api/v1/auth/session") {
      await json(route, { authenticated: true, expiresAt: "2030-01-01T00:00:00Z", username: "admin" });
      return true;
    }
    if (path === "/api/v1/control/lease") {
      await json(route, { actorId: "admin", clientId: "browser", expiresAt: "2030-01-01T00:01:00Z", leaseToken: "lease-1" }, 201);
      return true;
    }
    if (path === "/api/v1/global-knowledge/files/tree") {
      await json(route, { entries: [
        { kind: "file", modifiedAt: "2026-08-04T00:00:00Z", name: "enterprise.md", path: "enterprise.md", revision, size: 64 },
      ] });
      return true;
    }
    if (path === "/api/v1/global-knowledge/files/preview") {
      await json(route, { content: "# 企业标准\n优先使用全局知识。", kind: "markdown", path: url.searchParams.get("path") ?? "", truncated: false });
      return true;
    }
    return false;
  });
}

test("shows global knowledge with browser-local upload controls and no server picker", async ({ page }) => {
  await installKnowledgeServer(page);

  await page.addInitScript(() => window.sessionStorage.clear());
  await page.goto("/knowledge");

  await expect(page.getByRole("heading", { name: "全局知识库" })).toBeVisible();
  await expect(page.getByText("服务器文件")).toHaveCount(0);
  await expect(page.getByText(".manyselves")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "上传文件" })).toBeVisible();
  await expect(page.locator('input[type="file"]')).toHaveAttribute("multiple", "");
  await page.getByRole("button", { name: "预览 enterprise.md" }).click();
  await expect(page.getByRole("heading", { name: "企业标准" })).toBeVisible();
});
