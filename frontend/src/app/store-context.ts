import { createContext, use } from "react";
import { useStore } from "zustand";

import type {
  ConnectionStore,
  ConnectionStoreState,
} from "../store/connection-store";
import type { WorkspaceStore, WorkspaceStoreState } from "../store/workspace-store";

export const ConnectionStoreContext = createContext<ConnectionStore | null>(null);
export const WorkspaceStoreContext = createContext<WorkspaceStore | null>(null);

export function useConnectionStore<T>(
  selector: (state: ConnectionStoreState) => T,
): T {
  const store = use(ConnectionStoreContext);
  if (!store) {
    throw new Error("useConnectionStore must be used within AppProviders");
  }
  return useStore(store, selector);
}

export function useWorkspaceStore<T>(selector: (state: WorkspaceStoreState) => T): T {
  const store = use(WorkspaceStoreContext);
  if (!store) {
    throw new Error("useWorkspaceStore must be used within AppProviders");
  }
  return useStore(store, selector);
}
