import { describe, expect, it } from "vitest";

import type { RuntimeEvent } from "../../api/event-stream";
import type { components } from "../../api/generated/schema";
import { createAgentStore } from "./agent-store";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

const runtime: RuntimeSnapshot = {
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
};

function statusEvent(sequence: number, status: string): RuntimeEvent {
  return {
    eventId: `stream-a:evt-${sequence}`,
    payload: { agent_type: "main", status },
    schemaVersion: 1,
    sequence,
    streamId: "stream-a",
    timestamp: "2026-08-03T08:00:00Z",
    type: "agent.status.changed",
  };
}

describe("agent store", () => {
  it("hydrates from the bootstrap snapshot and applies events deterministically", () => {
    const store = createAgentStore(runtime, "stream-a");

    store.getState().applyEvent(statusEvent(10, "running"));

    expect(store.getState().agents.main?.status).toBe("running");
    expect(store.getState().lastSequence).toBe(10);
  });

  it("clears stale event state when a new bootstrap stream is hydrated", () => {
    const store = createAgentStore(runtime, "stream-a");
    store.getState().applyEvent(statusEvent(3, "running"));
    store.getState().applyEvent(statusEvent(5, "idle"));

    expect(store.getState().refreshRequested).toBe(true);
    store.getState().hydrate(runtime, "stream-b");

    expect(store.getState()).toMatchObject({
      lastSequence: null,
      refreshRequested: false,
      stale: false,
      streamId: "stream-b",
    });
  });
});
