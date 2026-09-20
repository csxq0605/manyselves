import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";
import { RuntimePage, RuntimeRoutePage } from "./RuntimePage";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];
type WorkflowRunResponse = components["schemas"]["WorkflowRunResponse"];

const snapshot: RuntimeSnapshot = {
  active_session_id: "session-1",
  agent_statuses: { main: "running", researcher: "waiting" },
  checkpoints: [],
  controller_client_id: null,
  debug: [{
    agentId: "main",
    duration_ms: 850,
    error: null,
    model: "provider-model",
    status: "success",
    timestamp: "2026-08-04T08:00:00Z",
    tokens_in: 120,
    tokens_out: 45,
  }],
  queues: [{ agent_id: "researcher", pending_count: 1, queued_messages: ["分析数据"] }],
  ready: true,
  tasks: [{
    blocking: false,
    brief: "生成能源分析报告",
    session_id: "session-1",
    source_agent: "main",
    status: "running",
    target_agent: "researcher",
    task_id: "task-1",
  }],
  tools: [{
    agentId: "researcher",
    arguments: { path: "Inputs/energy.xlsx" },
    error: null,
    name: "read_file",
    result: null,
    status: "running",
    toolCallId: "tool-1",
  }],
  workspace: null,
};

const activeWorkflowRun: WorkflowRunResponse = {
  run: {
    active: true,
    capabilityId: "distribution-reporting",
    runId: "full-report-live",
    status: "running",
    taskId: null,
    workflowId: "full-report",
  },
  state: {
    next_action_id: "run-module-cohort",
    next_action_index: 9,
    status: "running",
  },
  waitingInput: [],
};

describe("RuntimePage", () => {
  it("renders a cockpit summary for tasks, agents, tools, and errors", () => {
    render(<RuntimePage runs={[activeWorkflowRun]} snapshot={snapshot} />);

    expect(screen.getByText("\u8fd0\u884c\u9a7e\u9a76\u8231")).toBeVisible();
    expect(screen.getByText("进行中运行")).toBeVisible();
    expect(screen.getByText("1 个运行进行中")).toBeVisible();
    expect(screen.getByText("运行完成度")).toBeVisible();
    expect(screen.getByText("0 / 1")).toBeVisible();
    expect(screen.getByText("Agent \u5728\u7ebf\u72b6\u6001")).toBeVisible();
    expect(screen.getByText("\u5de5\u5177\u4e0e\u9519\u8bef")).toBeVisible();
  });

  it("shows the sanitized runtime projection without filesystem controls", () => {
    render(<RuntimePage runs={[activeWorkflowRun]} snapshot={snapshot} />);

    expect(screen.getByRole("heading", { name: "运行态" })).toBeVisible();
    expect(screen.getByText("生成能源分析报告")).toBeVisible();
    expect(screen.getByText("researcher · 1")).toBeVisible();
    expect(screen.getByText("read_file")).toBeVisible();
    expect(screen.getByText("1 个运行进行中")).toBeVisible();
    expect(screen.queryByRole("button", { name: /上传|新建目录|重命名|编辑|删除/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Work\/|\.manyselves/)).not.toBeInTheDocument();
  });

  it("shows failed tools as errors without exposing raw debug structures", () => {
    render(<RuntimePage snapshot={{
      ...snapshot,
      debug: [{ ...snapshot.debug[0]!, error: { authorization: "[REDACTED]" }, status: "error" }],
      tools: [{ ...snapshot.tools[0]!, error: "读取失败", status: "failed" }],
    }} />);

    expect(screen.getByRole("heading", { name: "错误" })).toBeVisible();
    expect(screen.getByText("read_file：读取失败")).toBeVisible();
    expect(screen.queryByText("authorization")).not.toBeInTheDocument();
  });

  it("shows active Workflow Runs even when the Main Agent Task Board is empty", () => {
    render(<RuntimePage
      runs={[activeWorkflowRun]}
      snapshot={{ ...snapshot, tasks: [] }}
    />);

    expect(screen.getByText("声明式运行")).toBeVisible();
    expect(screen.getByText("1 个运行进行中")).toBeVisible();
    expect(screen.getByText("配电安全报告")).toBeVisible();
    expect(screen.getByText("模块协同与写作")).toBeVisible();
    expect(screen.getByText("已完成 9 个顶层步骤")).toBeVisible();
    expect(screen.getByText("full-report-live")).toBeVisible();
    expect(screen.getByText("当前没有对话协作任务")).toBeVisible();
  });

  it("loads project Workflow Runs into the runtime route", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: true, description: "", displayName: "success", id: "success", revision: "r1" }],
      };
      if (path === "/api/v1/runs") return { runs: [activeWorkflowRun] };
      throw new Error(`unexpected path: ${path}`);
    });
    const gateway = {
      bootstrap: async () => ({
        project: { active: true, description: "", displayName: "success", id: "success", revision: "r1" },
        runtime: { ...snapshot, tasks: [] },
      }),
      requestJson,
    } as unknown as ApiGateway;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/success/runtime"]}>
        <Routes>
          <Route path="/projects/:projectId/runtime" element={<RuntimeRoutePage gateway={gateway} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>);

    expect(await screen.findByText("full-report-live")).toBeVisible();
    expect(requestJson).toHaveBeenCalledWith("/api/v1/runs");
    expect(screen.getByText("1 个运行进行中")).toBeVisible();
  });
});
