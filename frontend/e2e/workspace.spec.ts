import { expect, test, type Page, type Route } from "@playwright/test";

const revision = "a".repeat(64);
const files = [
  { kind: "file", modifiedAt: "2026-08-02T00:00:00Z", name: "notes.md", path: "notes.md", revision, size: 12 },
  { kind: "file", modifiedAt: "2026-08-02T00:00:00Z", name: "data.csv", path: "data.csv", revision, size: 8 },
  { kind: "file", modifiedAt: "2026-08-02T00:00:00Z", name: "script.py", path: "script.py", revision, size: 14 },
] as const;

const bootstrap = {
  agents: { main: "Main Agent" }, conversations: [], maintenance: {}, project: { id: "project-1" },
  runtime: { active_session_id: "session-1", agent_statuses: { main: "idle" }, checkpoints: [],
    controller_client_id: null, debug: [], queues: [], ready: true, tasks: [], tools: [], workspace: null },
  settings: { control_lease_seconds: 30, sse_client_queue_capacity: 128, sse_replay_capacity: 512 },
  streamId: "stream-1",
};

async function json(route: Route, body: unknown) {
  await route.fulfill({ body: JSON.stringify(body), contentType: "application/json", status: 200 });
}

async function mockRuntime(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/auth/session") {
      return json(route, { authenticated: true, expiresAt: "2030-01-01T00:00:00Z", username: "admin" });
    }
    if (path === "/api/v1/bootstrap") return json(route, bootstrap);
    if (path === "/api/v1/events") {
      return route.fulfill({ body: "", contentType: "text/event-stream", status: 200 });
    }
    if (path === "/api/v1/projects" && request.method() === "GET") {
      return json(route, { projects: [{ active: true, description: "", displayName: "Energy", id: "project-1", revision }] });
    }
    if (path === "/api/v1/global-knowledge/files/tree") {
      return json(route, { entries: [{
        kind: "file", modifiedAt: "2026-08-04T00:00:00Z", name: "standard.md",
        path: "standard.md", revision, size: 18,
      }] });
    }
    if (path === "/api/v1/global-knowledge/files/preview") {
      return json(route, { content: "# Shared standard", kind: "markdown", path: "standard.md", truncated: false });
    }
    if (path.endsWith("/files/tree")) {
      const root = url.searchParams.get("path") ?? "";
      return json(route, { entries: files.map((entry) => ({
        ...entry,
        path: root ? `${root}/${entry.name}` : entry.name,
      })) });
    }
    if (path.endsWith("/files/content") && request.method() === "GET") {
      const filePath = url.searchParams.get("path") ?? "";
      const content = filePath === "script.py" ? "print('ready')" : "# Notes\nhello";
      return json(route, { content, modifiedAt: "2026-08-02T00:00:00Z", path: filePath, revision, size: content.length });
    }
    if (path.endsWith("/files/preview")) {
      const filePath = url.searchParams.get("path");
      if (filePath?.endsWith("data.csv")) {
        return json(route, { kind: "spreadsheet", path: filePath, sheets: [
          { columnCount: 2, name: "CSV", rowCount: 2, rows: [["meter", "value"], ["A-1", "42"]], truncated: false },
        ] });
      }
      return json(route, { content: "# Notes\nhello", kind: "markdown", path: filePath, truncated: false });
    }
    if (path === "/api/v1/operations/python") {
      return json(route, { commandId: "00000000-0000-4000-8000-000000000001", operationId: "op-1", status: "accepted" });
    }
    if (path === "/api/v1/operations/op-1/interrupt") {
      return json(route, { arguments: [], completedAt: new Date().toISOString(), operationId: "op-1",
        path: "script.py", returnCode: null, startedAt: new Date().toISOString(), status: "interrupted",
        stderr: "", stderrTruncated: false, stdout: "ready", stdoutTruncated: false });
    }
    if (path === "/api/v1/operations/op-1") {
      return json(route, { arguments: [], completedAt: null, operationId: "op-1", path: "script.py",
        returnCode: null, startedAt: new Date().toISOString(), status: "running", stderr: "",
        stderrTruncated: false, stdout: "ready", stdoutTruncated: false });
    }
    return route.fulfill({ body: `Unhandled ${request.method()} ${path}`, status: 404 });
  });
}

test("opens a project knowledge directory and previews a bounded spreadsheet", async ({ page }) => {
  await mockRuntime(page);
  await page.goto("/projects/project-1/knowledge");

  await expect(page.getByRole("heading", { name: "知识库" })).toBeVisible();
  await page.getByRole("button", { name: "预览 data.csv" }).click();
  await expect(page.getByRole("heading", { name: "Knowledge/data.csv" })).toBeVisible();
  await expect(page.getByText("A-1")).toBeVisible();
  await expect(page.getByText("2 行 × 2 列")).toBeVisible();
  await page.getByRole("button", { name: "关闭预览" }).click();
  await expect(page.getByRole("button", { name: "预览 data.csv" })).toBeVisible();
});

test("opens global knowledge as a real browser-file workspace", async ({ page }) => {
  await mockRuntime(page);
  await page.goto("/");

  await page.getByRole("link", { name: "全局知识库" }).click();
  await expect(page.getByRole("heading", { name: "全局知识库" })).toBeVisible();
  await expect(page.getByRole("button", { name: "上传文件" })).toBeVisible();
  await expect(page.getByText("服务器文件")).toHaveCount(0);
  await page.getByRole("button", { name: "预览 standard.md" }).click();
  await expect(page.getByRole("heading", { name: "Shared standard" })).toBeVisible();
});

test("opens a project text file in the shared editor", async ({ page }) => {
  await mockRuntime(page);
  await page.goto("/projects/project-1/inputs");

  await page.getByRole("button", { name: "编辑 notes.md" }).click();
  await expect(page.getByRole("tab", { name: "notes.md" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("Inputs/notes.md", { exact: true })).toBeVisible();
});
