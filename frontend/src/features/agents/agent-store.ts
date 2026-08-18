import { createStore } from "zustand/vanilla";

import type { RuntimeEvent } from "../../api/event-stream";
import type { components } from "../../api/generated/schema";
import {
  emptyAgentState,
  hydrateAgentState,
  reduceAgentEvent,
  type AgentEventState,
} from "./event-reducer";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

export interface AgentStoreState extends AgentEventState {
  readonly applyEvent: (event: RuntimeEvent) => void;
  readonly hydrate: (snapshot: RuntimeSnapshot, streamId: string) => void;
}

function withActions(
  state: AgentEventState,
  applyEvent: AgentStoreState["applyEvent"],
  hydrate: AgentStoreState["hydrate"],
): AgentStoreState {
  return { ...state, applyEvent, hydrate };
}

export function createAgentStore(snapshot?: RuntimeSnapshot, streamId?: string) {
  return createStore<AgentStoreState>()((set) => {
    const applyEvent = (event: RuntimeEvent) => {
      set((current) => withActions(reduceAgentEvent(current, event), applyEvent, hydrate));
    };
    const hydrate = (nextSnapshot: RuntimeSnapshot, nextStreamId: string) => {
      set(withActions(hydrateAgentState(nextSnapshot, nextStreamId), applyEvent, hydrate));
    };
    const initial = snapshot && streamId
      ? hydrateAgentState(snapshot, streamId)
      : emptyAgentState(streamId ?? null);
    return withActions(initial, applyEvent, hydrate);
  });
}

export type AgentStore = ReturnType<typeof createAgentStore>;
