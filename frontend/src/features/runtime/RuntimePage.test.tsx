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
  spans: {
    "report-ledger/judge-modules": { end: "2026-10-23T14:12:10Z", start: "2026-10-23T14:09:00Z" },
    "report-ledger/prepare": { end: "2026-10-23T14:09:00Z", start: "2026-10-23T14:08:40Z" },
  },
  status: "running",
  workflowId: "full-report",
};

describe("RuntimePage", () => {
  it("shows the Run as a short list of stages, the running one with one line on its work", () => {
    render(<RuntimePage progress={progress} runs={[activeWorkflowRun]} snapshot={{ ...snapshot, tasks: [] }} />);

    const stages = screen.getAllByRole("listitem").filter((item) => item.classList.contains("run-stages__stage"));
    expect(stages.map((item) => item.querySelector(".run-stages__label")?.textContent)).toEqual([
      "准备资料", "读照片", "判断", "关联", "审查", "返修", "写作与核对", "修订收尾",
    ]);
    const byLabel = (label: string) => stages.find((item) => item.textContent?.startsWith(label))!;
    expect(byLabel("判断")).toHaveTextContent("完成");
    expect(byLabel("判断")).toHaveTextContent("3:30");
    expect(byLabel("写作与核对")).toHaveTextContent("进行中");
    expect(byLabel("写作与核对")).toHaveTextContent("模块写作：5 个写作组中 3 个完成");
    expect(byLabel("修订收尾")).toHaveTextContent("等待");
    expect(document.querySelector(".run-stages__pill")).toHaveTextContent("运行中");
    // The Main-era cards are gone.
    expect(screen.queryByText("对话协作任务")).not.toBeInTheDocument();
    expect(screen.queryByText("声明式运行")).not.toBeInTheDocument();
  });

  it("lists at most the three latest errors and hides the card when there are none", () => {
    const { rerender } = render(<RuntimePage progress={progress} runs={[activeWorkflowRun]} snapshot={{ ...snapshot, tasks: [] }} />);
    expect(screen.getByRole("heading", { name: "最新错误" })).toBeVisible();
    expect(screen.getByText("各模块判断 · G2：模型没有给出可用结果")).toBeVisible();

    rerender(<RuntimePage
      progress={{ ...progress, events: [] }}
      runs={[activeWorkflowRun]}
      snapshot={{
        ...snapshot,
        tools: ["a", "b", "c", "d"].map((name) => ({ ...snapshot.tools[0]!, error: `${name} 失败`, name, status: "failed" as const, toolCallId: name })),
      }}
    />);
    const errors = screen.getByRole("heading", { name: "最新错误" }).closest("section")!;
    expect(within(errors).getAllByRole("listitem")).toHaveLength(3);
    expect(within(errors).getAllByRole("listitem")[0]).toHaveTextContent("d：d 失败");

    rerender(<RuntimePage progress={{ ...progress, events: [] }} runs={[activeWorkflowRun]} snapshot={{ ...snapshot, tools: [] }} />);
    expect(screen.queryByRole("heading", { name: "最新错误" })).not.toBeInTheDocument();
  });

  it("folds inner steps, Agents and tool calls away, newest calls first and Agents by role", () => {
    render(<RuntimePage
      progress={progress}
      runs={[activeWorkflowRun]}
      snapshot={{
        ...snapshot,
        agent_statuses: { main: "idle", [writer]: "thinking" },
        tools: [
          { ...snapshot.tools[0]!, name: "older_call", status: "completed", toolCallId: "t-1" },
          { ...snapshot.tools[0]!, agentId: writer, name: "submit_result", status: "running", toolCallId: "t-2" },
        ],
      }}
    />);

    const details = screen.getByText("内部步骤与运行明细").closest("details")!;
    expect(details).not.toHaveAttribute("open");
    expect(within(details).getByText("report-consultant · writer · 2.4#1")).toBeInTheDocument();
    const tools = within(details).getByRole("heading", { name: "工具调用（最新在上）" }).closest("section")!;
    expect(within(tools).getAllByRole("listitem")[0]).toHaveTextContent("submit_result");
    expect(screen.queryByText(/Work\/|\.manyselves/)).not.toBeInTheDocument();
  });

  it("says so when there is no Run", () => {
    render(<RuntimePage snapshot={snapshot} />);
    expect(screen.getByText("当前没有运行")).toBeVisible();
  });

  it("loads project Workflow Runs into the runtime route", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: true, description: "", displayName: "success", id: "success", revision: "r1" }],
      };
      if (path === "/api/v1/runs") return { runs: [activeWorkflowRun] };
      if (path === "/api/v1/runs/full-report-live/progress") return {
        ...progress, root: null, spans: {},
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

    expect(await screen.findByRole("list", { name: "运行阶段" })).toBeVisible();
    expect(requestJson).toHaveBeenCalledWith("/api/v1/runs");
    expect(requestJson).toHaveBeenCalledWith("/api/v1/runs/full-report-live/progress");
  });
});
