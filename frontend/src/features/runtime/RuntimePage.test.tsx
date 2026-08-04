import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/schema";
import { RuntimePage } from "./RuntimePage";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

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

describe("RuntimePage", () => {
  it("shows the sanitized runtime projection without filesystem controls", () => {
    render(<RuntimePage snapshot={snapshot} />);

    expect(screen.getByRole("heading", { name: "运行态" })).toBeVisible();
    expect(screen.getByText("生成能源分析报告")).toBeVisible();
    expect(screen.getByText("researcher · 1")).toBeVisible();
    expect(screen.getByText("read_file")).toBeVisible();
    expect(screen.getByText("1 个任务进行中")).toBeVisible();
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
});
