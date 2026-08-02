import type { components } from "../../api/generated/schema";
import { createStore } from "zustand/vanilla";

export type ServerFile = components["schemas"]["FileContent"];

export interface EditorPosition {
  readonly column: number;
  readonly lineNumber: number;
}

export interface EditorTab {
  readonly cursor: EditorPosition | null;
  readonly dirty: boolean;
  readonly draftContent: string;
  readonly externalRevision: string | null;
  readonly path: string;
  readonly projectId: string;
  readonly serverContent: string;
  readonly serverRevision: string;
  readonly viewState: unknown;
}

export interface EditorStoreState {
  readonly activePath: string | null;
  readonly activateTab: (path: string) => void;
  readonly applySaved: (file: ServerFile) => void;
  readonly closeTab: (path: string) => void;
  readonly markServerChanged: (path: string, revision: string) => void;
  readonly openFile: (projectId: string, file: ServerFile) => void;
  readonly reloadFromServer: (file: ServerFile) => void;
  readonly reset: () => void;
  readonly restoreDraft: (
    projectId: string,
    file: ServerFile,
    draftContent: string,
    baseRevision: string,
  ) => void;
  readonly setViewState: (path: string, cursor: EditorPosition | null, viewState: unknown) => void;
  readonly tabs: readonly EditorTab[];
  readonly updateDraft: (path: string, content: string) => void;
}

function fromServer(projectId: string, file: ServerFile): EditorTab {
  return {
    cursor: null,
    dirty: false,
    draftContent: file.content,
    externalRevision: null,
    path: file.path,
    projectId,
    serverContent: file.content,
    serverRevision: file.revision,
    viewState: null,
  };
}

export function createEditorStore(initialTabs: readonly EditorTab[] = []) {
  return createStore<EditorStoreState>()((set) => ({
    activePath: initialTabs.at(-1)?.path ?? null,
    activateTab: (path) => set({ activePath: path }),
    applySaved: (file) => set((state) => ({
      tabs: state.tabs.map((tab) => {
        if (tab.path !== file.path) return tab;
        const draftMatchesSavedContent = tab.draftContent === file.content;
        return {
          ...tab,
          dirty: !draftMatchesSavedContent,
          draftContent: draftMatchesSavedContent ? file.content : tab.draftContent,
          externalRevision: null,
          serverContent: file.content,
          serverRevision: file.revision,
        };
      }),
    })),
    closeTab: (path) => set((state) => {
      const index = state.tabs.findIndex((tab) => tab.path === path);
      const tabs = state.tabs.filter((tab) => tab.path !== path);
      const nextActive = state.activePath === path
        ? tabs[Math.min(Math.max(index, 0), tabs.length - 1)]?.path ?? null
        : state.activePath;
      return { activePath: nextActive, tabs };
    }),
    markServerChanged: (path, revision) => set((state) => ({
      tabs: state.tabs.map((tab) => tab.path === path && revision !== tab.serverRevision
        ? { ...tab, externalRevision: revision }
        : tab),
    })),
    openFile: (projectId, file) => set((state) => {
      const existing = state.tabs.find((tab) => tab.path === file.path);
      if (!existing) {
        return { activePath: file.path, tabs: [...state.tabs, fromServer(projectId, file)] };
      }
      const tabs = state.tabs.map((tab) => {
        if (tab.path !== file.path || file.revision === tab.serverRevision) return tab;
        if (tab.dirty) return { ...tab, externalRevision: file.revision };
        return fromServer(projectId, file);
      });
      return { activePath: file.path, tabs };
    }),
    reloadFromServer: (file) => set((state) => ({
      tabs: state.tabs.map((tab) => tab.path === file.path
        ? { ...fromServer(tab.projectId, file), cursor: tab.cursor, viewState: tab.viewState }
        : tab),
    })),
    reset: () => set({ activePath: null, tabs: [] }),
    restoreDraft: (projectId, file, draftContent, baseRevision) => set((state) => {
      const restored: EditorTab = {
        ...fromServer(projectId, file),
        dirty: draftContent !== file.content,
        draftContent,
        externalRevision: file.revision === baseRevision ? null : file.revision,
        serverRevision: baseRevision,
      };
      const exists = state.tabs.some((tab) => tab.path === file.path);
      return {
        activePath: file.path,
        tabs: exists
          ? state.tabs.map((tab) => tab.path === file.path ? restored : tab)
          : [...state.tabs, restored],
      };
    }),
    setViewState: (path, cursor, viewState) => set((state) => ({
      tabs: state.tabs.map((tab) => tab.path === path ? { ...tab, cursor, viewState } : tab),
    })),
    tabs: initialTabs,
    updateDraft: (path, content) => set((state) => ({
      tabs: state.tabs.map((tab) => tab.path === path
        ? { ...tab, dirty: content !== tab.serverContent, draftContent: content }
        : tab),
    })),
  }));
}

export type EditorStore = ReturnType<typeof createEditorStore>;
