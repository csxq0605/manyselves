import { createStore } from "zustand/vanilla";

export type ConnectionState =
  | "connecting"
  | "online"
  | "reconnecting"
  | "resyncing"
  | "offline"
  | "unauthorized";

export interface ConnectionStoreState {
  readonly state: ConnectionState;
  readonly setState: (state: ConnectionState) => void;
}

export function createConnectionStore(initialState: ConnectionState = "connecting") {
  return createStore<ConnectionStoreState>()((set) => ({
    setState: (state) => set({ state }),
    state: initialState,
  }));
}

export type ConnectionStore = ReturnType<typeof createConnectionStore>;
