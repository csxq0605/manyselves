import { describe, expect, it } from "vitest";

import type { RuntimeEvent } from "../../api/event-stream";
import type { components } from "../../api/generated/schema";
import {
  emptyAgentState,
  hydrateAgentState,
  reduceAgentEvent,
} from "./event-reducer";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

function event(
  sequence: number,
  type: string,
  payload: Record<string, unknown>,
  overrides: Partial<RuntimeEvent> = {},
): RuntimeEvent {
  return {
    eventId: `stream-a:evt-${sequence}`,
    payload,
    schemaVersion: 1,
    sequence,
    streamId: "stream-a",
    timestamp: `2026-08-03T08:00:${String(sequence).padStart(2, "0")}Z`,
    type,
    ...overrides,
  };
}

function snapshot(overrides: Partial<RuntimeSnapshot> = {}): RuntimeSnapshot {
  return {
    active_session_id: "session-1",
    agent_statuses: { main: "idle" },
    checkpoints: [],
    controller_client_id: null,
    debug: [],
    queues: [],
    ready: true,
    tasks: [],
    tools: [],
    workspace: "project-1",
    ...overrides,
  };
}

describe("agent event reducer", () => {
  it("combines deltas and finalizes a message exactly once", () => {
    const deltaOne = event(1, "agent.message.delta", { content: "hel", thinking: "plan" }, {
      agentId: "MAIN",
      messageId: "message-1",
    });
    const deltaTwo = event(2, "agent.message.delta", { content: "lo", thinking: "ning" }, {
      agentId: "main",
      messageId: "message-1",
    });
    const completed = event(3, "agent.message.completed", { content: "hello", thinking: "planning" }, {
      agentId: "main",
      messageId: "message-1",
    });

    const state = [deltaOne, deltaTwo, completed].reduce(
      reduceAgentEvent,
      emptyAgentState("stream-a"),
    );

    expect(state.messages["message-1"]).toMatchObject({
      agentId: "main",
      content: "hello",
      status: "completed",
      thinking: "planning",
    });
    expect(reduceAgentEvent(state, completed)).toBe(state);
  });

  it("preserves accumulated answer and thinking when completion is only a terminal marker", () => {
    const streamed = reduceAgentEvent(
      emptyAgentState("stream-a"),
      event(1, "agent.message.delta", { content: "answer", thinking: "reasoning" }, {
        messageId: "message-1",
      }),
    );
    const completed = reduceAgentEvent(
      streamed,
      event(2, "agent.message.completed", { content: "", thinking: null }, {
        messageId: "message-1",
      }),
    );

    expect(completed.messages["message-1"]).toMatchObject({
      content: "answer",
      status: "completed",
      thinking: "reasoning",
    });
  });

  it("marks an unexplained sequence gap stale but accepts a broker-coalesced range", () => {
    const first = reduceAgentEvent(
      emptyAgentState("stream-a"),
      event(1, "system.notice", { content: "ready" }),
    );
    const coalesced = reduceAgentEvent(
      first,
      event(3, "agent.message.delta", {
        content: "combined",
        delivery: { coalescedCount: 2, fromSequence: 2, toSequence: 3 },
      }, { messageId: "message-1" }),
    );
    const gap = reduceAgentEvent(
      coalesced,
      event(5, "agent.status.changed", { agent_type: "main", status: "running" }),
    );

    expect(coalesced.stale).toBe(false);
    expect(coalesced.lastSequence).toBe(3);
    expect(gap.stale).toBe(true);
    expect(gap.refreshRequested).toBe(true);
    expect(gap.lastSequence).toBe(3);
  });

  it("keeps tool failures separate from runtime failures and masks secrets", () => {
    const toolStarted = event(1, "tool.started", {
      agentId: "Researcher",
      arguments: { authorization: "Bearer provider-secret", path: "notes.md" },
      toolCallId: "tool-1",
      tool_name: "read_file",
    });
    const toolFailed = event(2, "tool.failed", {
      agentId: "Researcher",
      error: "api_key=very-secret-value",
      result: null,
      toolCallId: "tool-1",
      tool_name: "read_file",
    });

    const state = [toolStarted, toolFailed].reduce(
      reduceAgentEvent,
      emptyAgentState("stream-a"),
    );

    expect(state.tools["tool-1"]).toMatchObject({
      agentId: "researcher",
      error: "api_key=[REDACTED]",
      name: "read_file",
      status: "failed",
    });
    expect(state.tools["tool-1"]?.arguments).toEqual({
      authorization: "[REDACTED]",
      path: "notes.md",
    });
    expect(state.runtimeError).toBeNull();
  });

  it("records system errors and marks the emitting agent as failed", () => {
    const state = reduceAgentEvent(
      emptyAgentState("stream-a"),
      event(1, "system.error", {
        agent_type: "Researcher",
        message: "Provider unavailable",
        source: "agent",
      }),
    );

    expect(state.runtimeError).toMatchObject({
      agentId: "researcher",
      content: "Provider unavailable",
      kind: "error",
    });
    expect(state.agents.researcher?.status).toBe("error");
  });

  it("tracks dynamic agents, queue state, tasks, debug usage, waiting, and interrupt", () => {
    const events = [
      event(1, "agent.status.changed", { agent_type: "Analyst", status: "thinking" }),
      event(2, "queue.changed", { agent_type: "Analyst", queued_messages: ["next", "later"] }),
      event(3, "task.status.changed", {
        action: "started",
        brief: "Review evidence",
        source_agent: "main",
        target_agent: "Analyst",
        task_id: "task-1",
      }),
      event(4, "debug.api.completed", {
        agentId: "Analyst",
        duration_ms: 125,
        model: "model-a",
        status: "success",
        tokens_in: 10,
        tokens_out: 4,
      }),
      event(5, "system.notice", { agent_type: "Analyst", content: "Waiting for input" }),
      event(6, "system.interrupted", { agent_type: "Analyst", content: "Interrupted", kind: "interrupt" }),
    ];

    const state = events.reduce(reduceAgentEvent, emptyAgentState("stream-a"));

    expect(state.agents.analyst).toMatchObject({ id: "analyst", status: "waiting" });
    expect(state.queues.analyst).toMatchObject({ pendingCount: 2, items: ["next", "later"] });
    expect(state.tasks["task-1"]).toMatchObject({ status: "started", targetAgent: "analyst" });
    expect(state.debug).toHaveLength(1);
    expect(state.usage).toEqual({ durationMs: 125, tokensIn: 10, tokensOut: 4 });
    expect(state.interrupt).toMatchObject({ agentId: "analyst", active: true });
  });

  it("stores runtime errors independently and rebuilds cleanly from a reconnect snapshot", () => {
    const stale = reduceAgentEvent(
      reduceAgentEvent(emptyAgentState("stream-a"), event(7, "system.notice", { content: "first" })),
      event(9, "system.error", { content: "runtime failed", code: "RUNTIME_FAILURE" }),
    );
    const restored = hydrateAgentState(snapshot({
      agent_statuses: { main: "idle", Writer: "running" },
      queues: [{ agent_id: "Writer", pending_count: 1, queued_messages: ["draft"] }],
      tasks: [{
        blocking: false,
        brief: "Draft summary",
        source_agent: "main",
        status: "running",
        target_agent: "Writer",
        task_id: "task-2",
      }],
    }), "stream-b");

    expect(stale.stale).toBe(true);
    expect(restored.stale).toBe(false);
    expect(restored.refreshRequested).toBe(false);
    expect(restored.streamId).toBe("stream-b");
    expect(restored.agents.writer?.status).toBe("running");
    expect(restored.queues.writer?.pendingCount).toBe(1);
    expect(restored.tasks["task-2"]?.brief).toBe("Draft summary");
  });
});
