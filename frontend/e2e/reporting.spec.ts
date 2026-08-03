import { expect, test, type Page, type Route } from "@playwright/test";

const bootstrap = {
  agents: { main: "Main Agent" }, conversations: [], maintenance: {}, project: { id: "project-1" },
  runtime: { active_session_id: "session-1", agent_statuses: { main: "idle" }, checkpoints: [],
    controller_client_id: null, debug: [], queues: [], ready: true, tasks: [], tools: [], workspace: null },
  settings: { control_lease_seconds: 30, sse_client_queue_capacity: 128, sse_replay_capacity: 512 },
  streamId: "stream-reporting",
};

type Mode = "active" | "cancelled" | "completed" | "empty" | "failed" | "revision" | "waiting";

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ body: JSON.stringify(body), contentType: "application/json", status });
}

function run(mode: Mode, runId = "run-1") {
  const status = mode === "active" ? "running"
    : mode === "cancelled" ? "cancelled"
      : mode === "failed" ? "failed"
        : mode === "waiting" ? "needs_user_decision" : "completed";
  return {
    active: mode === "active",
    ...(mode === "failed" ? { error: "output verification failed" } : {}),
    run_id: runId,
    status,
  };
}

function snapshot(mode: Mode, runId = "run-1") {
  const completed = mode === "completed" || mode === "revision";
  const failed = mode === "failed";
  return {
    checkpoint: {
      activity: completed ? "delivery" : failed ? "delivery" : "dispatch",
      completed_modules: completed ? ["2.1", "2.2", "2.3", "2.4", "2.5"] : ["2.1"],
      preparation_refs: {
        coverage: `Work/runs/${runId}/context/coverage.json`,
        source_ledger: `Work/runs/${runId}/context/source-ledger.json`,
      },
      run_id: runId,
      specialist_modules: ["2.1", "2.4"],
      status: completed ? "completed" : failed ? "failed" : "in_progress",
    },
    evidence: { selected_action: mode === "waiting" ? null : "supplement" },
    outputs: completed ? [{
      exists: true, path: "Outputs/Reports/report.docx", sha256: "verified-sha", size: 2048,
    }] : failed ? [{
      exists: false, path: "Outputs/Reports/broken.docx", sha256: null, size: 0,
    }] : [],
    revision: mode === "revision"
      ? { baseline_version_id: "run-1", feedback: "修正保护边界", target_module_ids: ["2.2"] }
      : {},
    run: run(mode, runId),
    state: {
      activity: completed ? "delivery" : "dispatch",
      completed_modules: completed ? ["2.1", "2.2", "2.3", "2.4", "2.5"] : ["2.1"],
      preparation_refs: {
        coverage: `Work/runs/${runId}/context/coverage.json`,
        source_ledger: `Work/runs/${runId}/context/source-ledger.json`,
      },
      specialist_modules: ["2.1", "2.4"],
      status: completed ? "completed" : failed ? "failed" : "in_progress",
    },
    waitingInput: mode === "waiting" ? [{
      affected_modules: ["2.3"],
      allowed_actions: ["supplement", "draft", "skip", "stop"],
      decision_id: "decision-1",
      missing_items: ["缺少保护定值单"],
      status: "pending",
    }] : [],
  };
}

async function mockReporting(page: Page, initialMode: Mode) {
  let mode = initialMode;
  let activeRunId = "run-1";
  let eventRequests = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/bootstrap") return json(route, bootstrap);
    if (path === "/api/v1/events") {
      eventRequests += 1;
      return route.fulfill({ body: "", contentType: "text/event-stream", status: 200 });
    }
    if (path === "/api/v1/projects" && request.method() === "GET") {
      return json(route, { projects: [{ active: true, id: "project-1" }] });
    }
    if (path.endsWith("/files/tree")) return json(route, { entries: [] });
    if (path.endsWith("/files/download")) {
      return route.fulfill({ body: "docx", contentType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", status: 200 });
    }
    if (path === "/api/v1/conversations") {
      return json(route, { activeSessionId: "session-1", conversations: [] });
    }
    if (path === "/api/v1/conversations/messages") {
      return json(route, { messages: [], sessionId: "session-1" });
    }
    if (path === "/api/v1/reporting/runs" && request.method() === "GET") {
      return json(route, { runs: mode === "empty" ? [] : [run(mode, activeRunId)] });
    }
    if (path === "/api/v1/reporting/runs" && request.method() === "POST") {
      mode = "active";
      activeRunId = "run-new";
      return json(route, { commandId: "command-start", runId: activeRunId, status: "accepted", taskId: "task-new" }, 202);
    }
    if (path === "/api/v1/reporting/decisions/decision-1/resume") {
      mode = "completed";
      return json(route, { commandId: "command-decision", runId: activeRunId, status: "accepted" }, 202);
    }
    if (path.endsWith("/resume") && path.includes("/reporting/runs/")) {
      mode = "completed";
      return json(route, { commandId: "command-resume", runId: activeRunId, status: "accepted" }, 202);
    }
    if (path.endsWith("/cancel")) {
      mode = "cancelled";
      return json(route, { commandId: "command-cancel", runId: activeRunId, status: "accepted" }, 202);
    }
    if (path === "/api/v1/reporting/revisions") {
      mode = "revision";
      activeRunId = "run-revision";
      return json(route, { commandId: "command-revision", runId: activeRunId, status: "accepted" }, 202);
    }
    if (path.startsWith("/api/v1/reporting/runs/") && request.method() === "GET") {
      return json(route, snapshot(mode, decodeURIComponent(path.split("/").at(-1) ?? activeRunId)));
    }
    return route.fulfill({ body: `Unhandled ${request.method()} ${path}`, status: 404 });
  });
  return { eventRequests: () => eventRequests };
}

test("resolves waiting input, verifies delivery, downloads, refreshes and revises", async ({ page }) => {
  const server = await mockReporting(page, "waiting");
  await page.goto("/");
  await page.getByRole("button", { name: "报告中心" }).click();

  await expect(page.getByRole("heading", { name: "等待用户决定" })).toBeVisible();
  await page.getByLabel("补充信息").fill("保护定值单已上传至 Inputs");
  await page.getByRole("button", { name: "继续流程" }).click();
  await expect(page.getByText("服务端已验证交付文件")).toBeVisible();
  await expect(page.getByText("module-2.4-specialist")).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 report.docx" }).click();
  await expect((await download).suggestedFilename()).toBe("report.docx");

  await page.reload();
  await page.getByRole("button", { name: "报告中心" }).click();
  await expect(page.getByText("服务端已验证交付文件")).toBeVisible();
  await expect.poll(server.eventRequests).toBeGreaterThan(1);

  await page.getByLabel("目标模块").selectOption("2.2");
  await page.getByLabel("修订反馈").fill("修正保护边界");
  await page.getByRole("button", { name: "创建修订运行" }).click();
  await expect(page.getByRole("heading", { name: "报告运行 run-revision" })).toBeVisible();
});

test("keeps invalid output failed until a checkpoint resume succeeds", async ({ page }) => {
  await mockReporting(page, "failed");
  await page.goto("/");
  await page.getByRole("button", { name: "报告中心" }).click();

  await expect(page.getByText("output verification failed").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "下载 broken.docx" })).toHaveCount(0);
  await page.getByRole("button", { name: "从检查点恢复 / 重试" }).click();
  await expect(page.getByText("服务端已验证交付文件")).toBeVisible();
});

test("creates and cancels a report run through named commands", async ({ page }) => {
  await mockReporting(page, "empty");
  await page.goto("/");
  await page.getByRole("button", { name: "报告中心" }).click();

  await page.getByLabel("报告要求").fill("生成本项目供配电安全咨询报告");
  await page.getByRole("button", { name: "开始报告运行" }).click();
  await expect(page.getByRole("heading", { name: "报告运行 run-new" })).toBeVisible();
  await page.getByRole("button", { name: "取消运行" }).click();
  await expect(page.getByText("已取消").last()).toBeVisible();
});
