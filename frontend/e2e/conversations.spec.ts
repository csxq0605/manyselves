import { expect, test } from "@playwright/test";

import { installBaseServer, json } from "./fixtures/server";

test("uploads a browser file and sends only its project-bound logical reference", async ({ page }) => {
  let contextBody: unknown = null;
  let sentBody: unknown = null;
  await installBaseServer(page, async (route, path) => {
    const request = route.request();
    const url = new URL(request.url());
    if (path === "/api/v1/auth/session") {
      await json(route, { authenticated: true, expiresAt: "2030-01-01T00:00:00Z", username: "admin" });
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
    if (path === "/api/v1/conversations" && request.method() === "GET") {
      expect(url.searchParams.get("projectId")).toBe("project-1");
      await json(route, {
        activeSessionId: "session-1",
        conversations: [{
          active: true,
          name: "需求梳理",
          preview: "server",
          projectId: "project-1",
          sessionId: "session-1",
          timestamp: "2026-08-03T08:00:00Z",
        }],
        projectId: "project-1",
      });
      return true;
    }
    if (path === "/api/v1/conversations/messages") {
      expect(url.searchParams.get("projectId")).toBe("project-1");
      await json(route, { messages: [], projectId: "project-1", sessionId: "session-1" });
      return true;
    }
    if (path === "/api/v1/projects/project-1/files/upload") {
      expect(url.searchParams.get("path")).toBe("Inputs/brief.txt");
      expect(url.searchParams.get("conflict")).toBe("reject");
      await json(route, {
        kind: "file",
        modifiedAt: "2026-08-04T00:00:00Z",
        name: "brief.txt",
        path: "Inputs/brief.txt",
        revision: "a".repeat(64),
        size: 5,
      });
      return true;
    }
    if (path === "/api/v1/agents/main/file-context") {
      contextBody = request.postDataJSON();
      await json(route, { commandId: "context-command", status: "accepted" }, 202);
      return true;
    }
    if (path === "/api/v1/agents/main/messages") {
      sentBody = request.postDataJSON();
      await json(route, { commandId: "message-command", status: "accepted" }, 202);
      return true;
    }
    return false;
  });

  await page.goto("/projects/project-1/conversations/session-1");
  await expect(page.getByRole("heading", { name: "今天要处理什么？" })).toBeVisible();
  await expect(page.getByText("当前对话属于“能源管理”项目。Agent 会自动使用项目输入、知识库与输出模板。")).toBeVisible();
  await expect(page.getByRole("button", { name: "更多会话操作" })).toBeVisible();
  await expect(page.getByRole("button", { name: "新建会话" })).toHaveCount(0);
  await expect(page.getByText("文件将保存到项目“输入”")).toBeVisible();
  await page.getByLabel("选择本地文件").setInputFiles({
    buffer: Buffer.from("brief"),
    mimeType: "text/plain",
    name: "brief.txt",
  });
  await expect(page.getByRole("list", { name: "待发送附件" })).toContainText("brief.txt");
  await page.getByRole("textbox", { name: "消息" }).fill("检查能源基线");
  await page.getByRole("button", { name: "发送" }).click();

  await expect.poll(() => contextBody).toEqual({
    file: "Inputs/brief.txt",
    projectId: "project-1",
    type: "file",
  });
  await expect.poll(() => sentBody).toMatchObject({
    content: "检查能源基线",
    projectId: "project-1",
    source: "user",
  });
  expect(JSON.stringify([contextBody, sentBody])).not.toContain("C:\\");
});

test("returns an unauthorized bootstrap to the login page", async ({ page }) => {
  await installBaseServer(page, async (route, path) => {
    if (path === "/api/v1/auth/session") {
      await json(route, { authenticated: true, expiresAt: "2030-01-01T00:00:00Z", username: "admin" });
      return true;
    }
    if (path === "/api/v1/bootstrap") {
      await json(route, {
        error: { code: "UNAUTHORIZED", details: {}, message: "token required", retryable: false },
        requestId: "request-401",
      }, 401);
      return true;
    }
    return false;
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "登录 manyselves" })).toBeVisible();
  await expect(page.getByText("Agent 会话")).toHaveCount(0);
});
