import { expect, test, type Page, type Route } from "@playwright/test";

import { installBaseServer, json } from "./fixtures/server";

async function installOperationsServer(page: Page) {
  let outputExists = true;
  await installBaseServer(page, async (route: Route, path: string) => {
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
    if (path.endsWith("/files/tree") && url.searchParams.get("path") === "Outputs") {
      await json(route, { entries: outputExists ? [{
        kind: "file", modifiedAt: "2026-08-04T08:00:00Z", name: "energy-report.md",
        path: "Outputs/energy-report.md", revision: "a".repeat(64), size: 128,
      }] : [] });
      return true;
    }
    if (path.endsWith("/files/preview")) {
      await json(route, { content: "# 能源分析报告", kind: "markdown", path: "Outputs/energy-report.md", truncated: false });
      return true;
    }
    if (path.endsWith("/files/download")) {
      await route.fulfill({ body: "# 能源分析报告", contentType: "text/markdown", status: 200 });
      return true;
    }
    if (path.endsWith("/files/entries") && request.method() === "DELETE") {
      outputExists = false;
      await route.fulfill({ status: 204 });
      return true;
    }
    if (path === "/api/v1/events/logs") {
      expect(url.searchParams.get("projectId")).toBe("project-1");
      await json(route, { entries: [
        { agentId: "main", eventId: "stream-1:evt-1", level: "info", message: "任务开始", sessionId: "session-1", timestamp: "2026-08-04T08:00:00Z", type: "task.status.changed" },
        { agentId: "researcher", eventId: "stream-1:evt-2", level: "error", message: "工具失败", sessionId: "session-1", timestamp: "2026-08-04T08:01:00Z", type: "tool.failed" },
      ], projectId: "project-1" });
      return true;
    }
    return false;
  });
}

test("keeps project outputs limited to preview, download and delete", async ({ page }) => {
  await installOperationsServer(page);
  await page.goto("/projects/project-1/outputs");

  await expect(page.getByRole("heading", { name: "输出" })).toBeVisible();
  await expect(page.getByRole("button", { name: "上传本地文件" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /编辑 energy-report\.md/ })).toHaveCount(0);

  await page.getByRole("button", { name: "预览 energy-report.md" }).click();
  await expect(page.getByRole("heading", { name: "Outputs/energy-report.md" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "能源分析报告" })).toBeVisible();
  await page.getByRole("button", { name: "关闭预览" }).click();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 energy-report.md" }).click();
  await expect((await download).suggestedFilename()).toBe("energy-report.md");

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除 energy-report.md" }).click();
  await expect(page.getByText("此目录还没有文件")).toBeVisible();
});

test("shows live runtime and filterable sanitized project logs", async ({ page }) => {
  await installOperationsServer(page);
  await page.goto("/projects/project-1/runtime");

  await expect(page.getByRole("heading", { name: "运行态" })).toBeVisible();
  await expect(page.getByText("Review evidence")).toBeVisible();
  await expect(page.getByText("read_file")).toBeVisible();
  await expect(page.locator("#main-outlet").getByRole("button", { name: /上传|编辑|删除/ })).toHaveCount(0);

  await page.getByRole("link", { name: "日志" }).click();
  await expect(page.getByText("任务开始")).toBeVisible();
  await page.getByLabel("日志级别").selectOption("error");
  await expect(page.getByText("任务开始")).toHaveCount(0);
  await expect(page.getByText("工具失败")).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载日志" }).click();
  await expect((await download).suggestedFilename()).toBe("project-1-events.json");
});
