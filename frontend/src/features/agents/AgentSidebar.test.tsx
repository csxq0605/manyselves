import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { RuntimeEvent } from "../../api/event-stream";
import type { components } from "../../api/generated/schema";
import { AgentSidebar } from "./AgentSidebar";
import { hydrateAgentState, reduceAgentEvent } from "./event-reducer";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

const runtime: RuntimeSnapshot = {
  active_session_id: "session-1",
  agent_statuses: { main: "idle", Researcher: "thinking" },
  checkpoints: [{
    agent_id: "Researcher",
    checkpoint_id: "checkpoint-1",
    description: "Evidence collected",
    epoch: 3,
    message_id: "message-1",
    source: "agent",
    timestamp: "2026-08-03T08:00:00Z",
  }],
  controller_client_id: null,
  debug: [],
  queues: [{ agent_id: "Researcher", pending_count: 1, queued_messages: ["Verify source"] }],
  ready: true,
  tasks: [{
    blocking: true,
    brief: "Review evidence",
    session_id: "session-1",
    source_agent: "main",
    status: "running",
    target_agent: "Researcher",
    task_id: "task-1",
  }],
  tools: [],
  workspace: "project-1",
};

function event(sequence: number, type: string, payload: Record<string, unknown>): RuntimeEvent {
  return {
    agentId: "Researcher",
    eventId: `stream-a:evt-${sequence}`,
    messageId: "message-live",
    payload,
    schemaVersion: 1,
    sequence,
    streamId: "stream-a",
    timestamp: "2026-08-03T08:01:00Z",
    type,
  };
}

describe("AgentSidebar", () => {
  it("renders live public output separately from thinking and complete runtime state", async () => {
    const user = userEvent.setup();
    let state = hydrateAgentState(runtime, "stream-a");
    state = reduceAgentEvent(state, event(20, "agent.message.delta", {
      content: "Public answer",
      thinking: "Private reasoning summary",
    }));
    state = reduceAgentEvent(state, event(21, "system.notice", {
      content: "Waiting for approval",
    }));
    state = reduceAgentEvent(state, event(22, "system.interrupted", {
      content: "Interrupted by user",
      kind: "interrupt",
    }));

    render(<AgentSidebar state={state} />);

    expect(screen.getByRole("heading", { name: "Agent 运行态" })).toBeVisible();
    expect(within(screen.getByRole("list", { name: "Agent 状态" })).getByText("researcher")).toBeVisible();
    expect(screen.getByText("Verify source")).toBeVisible();
    expect(screen.getByText("Review evidence")).toBeVisible();
    expect(screen.getByText("Evidence collected")).toBeVisible();
    expect(screen.getByText("Interrupted by user")).toBeVisible();

    const answer = screen.getByRole("region", { name: "实时回答" });
    const thinking = screen.getByRole("region", { name: "Thinking" });
    expect(within(answer).getByText("Public answer")).toBeVisible();
    await user.click(within(thinking).getByText("researcher 的过程摘要"));
    expect(within(thinking).getByText("Private reasoning summary")).toBeVisible();
    expect(within(answer).queryByText("Private reasoning summary")).not.toBeInTheDocument();
  });

  it("shows stale runtime state without pretending it is current", () => {
    const state = {
      ...hydrateAgentState(runtime, "stream-a"),
      refreshRequested: true,
      stale: true,
    };

    render(<AgentSidebar state={state} />);

    expect(screen.getByRole("alert")).toHaveTextContent("运行态可能已过期");
  });
});
