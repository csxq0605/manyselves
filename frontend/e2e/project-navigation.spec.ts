import { expect, test } from "@playwright/test";

import { installBaseServer, json } from "./fixtures/server";

const revision = "b".repeat(64);
const project = { active: true, description: "", displayName: "能源管理", id: "project-1", revision };
const fileEntries = [
  { kind: "file", modifiedAt: "2026-08-04T00:00:00Z", name: "input.md", path: "Inputs/input.md", revision, size: 32 },
  { kind: "file", modifiedAt: "2026-08-04T00:00:00Z", name: "standard.md", path: "Knowledge/standard.md", revision, size: 32 },
  { kind: "file", modifiedAt: "2026-08-04T00:00:00Z", name: "report.md", path: "Templates/report.md", revision, size: 32 },
  { kind: "file", modifiedAt: "2026-08-04T00:00:00Z", name: "final.md", path: "Outputs/final.md", revision, size: 32 },
];

async function installProjectServer(page: Parameters<typeof installBaseServer>[0]) {
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
    if (path === "/api/v1/projects") {
      await json(route, { projects: [project] });
      return true;
    }
    if (path.endsWith("/files/tree")) {
      const root = url.searchParams.get("path") ?? "";
      await json(route, { entries: fileEntries.filter((entry) => !root || entry.path.startsWith(`${root}/`)) });
      return true;
    }
    if (path.endsWith("/files/preview")) {
      await json(route, { content: "# Preview", kind: "markdown", path: url.searchParams.get("path") ?? "", truncated: false });
      return true;
    }
    return false;
  });
}

test("shows the fixed project entries and keeps folders as navigation only", async ({ page }) => {
  await installProjectServer(page);

  await page.goto("/projects/project-1");

  for (const label of ["输入", "知识库", "输出模板", "输出", "运行态", "日志"]) {
    await expect(page.getByRole("link", { exact: true, name: label })).toBeVisible();
  }
  await expect(page.getByText(".manyselves")).toHaveCount(0);
  await expect(page.getByText("服务器文件")).toHaveCount(0);

  await page.getByRole("link", { exact: true, name: "输出模板" }).click();
  await expect(page).toHaveURL(/\/projects\/project-1\/templates$/);
  await expect(page.getByRole("heading", { name: "输出模板" })).toBeVisible();
  await expect(page.getByRole("button", { name: "上传本地文件" })).toBeVisible();
  await expect(page.getByRole("button", { name: "编辑 report.md" })).toBeVisible();

  await page.getByRole("link", { exact: true, name: "输出" }).click();
  await expect(page).toHaveURL(/\/projects\/project-1\/outputs$/);
  await expect(page.getByRole("heading", { name: "输出" })).toBeVisible();
  await expect(page.getByRole("button", { name: "上传本地文件" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "编辑 final.md" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "预览 final.md" })).toBeVisible();
  await expect(page.getByRole("button", { name: "下载 final.md" })).toBeVisible();
  await expect(page.getByRole("button", { name: "删除 final.md" })).toBeVisible();
});
