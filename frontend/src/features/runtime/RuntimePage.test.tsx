import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";
import type { RunProgressResponse } from "../runs/workflow-api";
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
    next_action_id: "run-report-ledger",
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
    expect(screen.getByText("判断、关联、审查、写作与总编")).toBeVisible();
    expect(screen.getByText("已完成 9 个顶层步骤")).toBeVisible();
    expect(screen.getByText("full-report-live")).toBeVisible();
    // The Main-era task board and queue only show when they hold something.
    expect(screen.queryByText("当前没有对话协作任务")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "运行进度" })).toBeVisible();
  });

  it("follows the running Run step by step, newest calls and events first", () => {
    const writer = "distribution-reporting:full-report-0123456789abcdef0123456789abcdef:report-consultant:run:full-report-0123456789abcdef0123456789abcdef:report-consultant:writer:2.4#1";
    const progress: RunProgressResponse = {
      events: [
        { action_id: "write-modules", agent_id: null, error: null, key: "2.3", kind: "map.item.completed", reused: null, tool_id: null, ts: "2026-10-23T14:29:59Z", workflow_id: "report-ledger" },
        { action_id: "judge-modules", agent_id: null, error: "模型没有给出可用结果", key: "G2", kind: "map.item.failed", reused: null, tool_id: null, ts: "2026-10-23T14:10:00Z", workflow_id: "report-ledger" },
      ],
      root: {
        current: "run-report-ledger",
        detail: {
          action_id: "run-report-ledger",
          child: {
            current: "draft",
            detail: {
              action_id: "draft",
              branches: [
                {
                  branch_id: "writing",
                  node: {
                    current: "write-modules",
                    detail: {
                      action_id: "write-modules",
                      completed: 3,
                      completed_keys: ["2.1", "2.2", "2.3"],
                      failed: 0,
                      items: [{
                        key: "2.4#1",
                        node: {
                          current: "write",
                          detail: { action_id: "each-section", index: 1, kind: "for_each", total: 4 },
                          status: "running",
                          steps: [{ id: "each-section", kind: "for_each", status: "pending" }, { id: "write", kind: "invoke_agent", status: "running" }],
                          workflow_id: "ledger-module-writing",
                        },
                        status: "running",
                      }],
                      kind: "map",
                      queued: 1,
                      queued_keys: ["2.5"],
                      running: 1,
                      total: 5,
                      workflow: "ledger-module-writing",
                    },
                    status: "running",
                    steps: [{ id: "write-modules", kind: "map", status: "running" }, { id: "check-report", kind: "invoke_tool", status: "pending" }],
                    workflow_id: "report-ledger",
                  },
                  start: "write-modules",
                  status: "running",
                },
                { branch_id: "chief", node: null, start: "chief-summary", status: "pending" },
              ],
              kind: "parallel",
            },
            status: "running",
            steps: [
              { id: "judge-modules", kind: "map", status: "completed" },
              { id: "draft", kind: "parallel", status: "running" },
            ],
            workflow_id: "report-ledger",
          },
          kind: "subworkflow",
        },
        status: "running",
        steps: [
          { id: "run-photo-reading", kind: "subworkflow", status: "completed" },
          { id: "run-report-ledger", kind: "subworkflow", status: "running" },
        ],
        workflow_id: "full-report",
      },
      runId: "full-report-live",
      status: "running",
      workflowId: "full-report",
    };
    render(<RuntimePage
      progress={progress}
      runs={[activeWorkflowRun]}
      snapshot={{
        ...snapshot,
        agent_statuses: { main: "idle", [writer]: "thinking" },
        tasks: [],
        tools: [
          { ...snapshot.tools[0]!, name: "older_call", status: "completed", toolCallId: "t-1" },
          { ...snapshot.tools[0]!, agentId: writer, name: "submit_result", status: "running", toolCallId: "t-2" },
        ],
      }}
    />);

    // The current stage, explained, and each parallel branch's own position.
    expect(screen.getByText("写正文与总编", { selector: ".run-progress__note strong" })).toBeVisible();
    expect(screen.getByText("各写作组写第二章，总编同时写第三章和第一章。")).toBeVisible();
    expect(screen.getByText(/5 个写作组：完成 3 · 进行中 1 · 排队 1/)).toBeVisible();
    expect(screen.getByText("写一节 · 第 2 / 4 项")).toBeVisible();
    expect(screen.getByText("排队：2.5")).toBeVisible();
    // Workflow Agents show by role and piece, not by their raw ids.
    expect(screen.getAllByText("report-consultant · writer · 2.4#1").length).toBeGreaterThan(0);
    expect(screen.getByText("思考中")).toBeVisible();
    // Newest first.
    const tools = screen.getByRole("heading", { name: "工具调用" }).closest("section")!;
    expect(within(tools).getAllByRole("listitem")[0]).toHaveTextContent("submit_result");
    const events = screen.getByRole("heading", { name: "运行动态（最新在上）" }).parentElement!;
    expect(within(events).getAllByRole("listitem")[0]).toHaveTextContent("模块写作：2.3 完成");
    // Run failures are errors too.
    expect(screen.getByText("各模块判断 · G2：模型没有给出可用结果")).toBeVisible();
  });

  it("loads project Workflow Runs into the runtime route", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: true, description: "", displayName: "success", id: "success", revision: "r1" }],
      };
      if (path === "/api/v1/runs") return { runs: [activeWorkflowRun] };
      if (path === "/api/v1/runs/full-report-live/progress") return {
        events: [], root: null, runId: "full-report-live", status: "running", workflowId: "full-report",
      };
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
    expect(await screen.findByText("还没有运行进度")).toBeVisible();
    expect(requestJson).toHaveBeenCalledWith("/api/v1/runs/full-report-live/progress");
    expect(screen.getByText("1 个运行进行中")).toBeVisible();
  });
});
