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
    if (path === "/api/v1/bootstrap") return json(route, bootstrap);
    if (path === "/api/v1/events") {
      return route.fulfill({ body: "", contentType: "text/event-stream", status: 200 });
    }
    if (path === "/api/v1/projects" && request.method() === "GET") {
      return json(route, { projects: [{ active: true, id: "project-1" }] });
    }
    if (path.endsWith("/files/tree")) return json(route, { entries: files });
    if (path.endsWith("/files/content") && request.method() === "GET") {
      const filePath = url.searchParams.get("path") ?? "";
      const content = filePath === "script.py" ? "print('ready')" : "# Notes\nhello";
      return json(route, { content, modifiedAt: "2026-08-02T00:00:00Z", path: filePath, revision, size: content.length });
    }
    if (path.endsWith("/files/preview")) {
      const filePath = url.searchParams.get("path");
      if (filePath === "data.csv") {
        return json(route, { kind: "spreadsheet", path: "data.csv", sheets: [
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

test("opens the server workspace and previews a bounded spreadsheet", async ({ page }) => {
  await mockRuntime(page);
  await page.goto("/");

  await expect(page.getByRole("combobox", { name: "项目" })).toHaveValue("project-1");
  await page.getByRole("treeitem", { name: "data.csv" }).click();
  await page.getByRole("button", { name: "预览" }).click();
  await expect(page.getByRole("heading", { name: "data.csv" })).toBeVisible();
  await expect(page.getByText("A-1")).toBeVisible();
  await expect(page.getByText("2 行 × 2 列")).toBeVisible();
  await page.getByRole("button", { name: "返回编辑器" }).click();
  await expect(page.getByRole("tab", { name: "data.csv" })).toHaveAttribute("aria-selected", "true");
});

test("runs and interrupts Python only after trusted-server confirmation", async ({ page }) => {
  await mockRuntime(page);
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto("/");

  await page.getByRole("treeitem", { name: "script.py" }).click();
  await expect(page.getByText("script.py", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "在可信服务器运行" }).click();
  await expect(page.getByText("ready", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "中断运行" }).click();
  await expect(page.getByRole("status", { name: "" }).filter({ hasText: "interrupted" })).toBeVisible();
});
