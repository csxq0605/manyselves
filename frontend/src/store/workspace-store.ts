import { createStore } from "zustand/vanilla";

export interface WorkspaceStoreState {
  readonly drafts: Readonly<Record<string, string>>;
  readonly setDraft: (path: string, content: string) => void;
}

export function createWorkspaceStore(initialDrafts: Readonly<Record<string, string>> = {}) {
  return createStore<WorkspaceStoreState>()((set) => ({
    drafts: initialDrafts,
    setDraft: (path, content) =>
      set((current) => ({
        drafts: { ...current.drafts, [path]: content },
      })),
  }));
}

export type WorkspaceStore = ReturnType<typeof createWorkspaceStore>;
