import { createStore } from "zustand/vanilla";

const STORAGE_KEY = "manyselves.fileTree.expanded.v1";

function isRelativeServerPath(path: string): boolean {
  return path.length > 0 && !path.startsWith("/") && !path.startsWith("\\") &&
    path.split(/[\\/]/).every((part) => part !== ".." && part !== "");
}

function restore(storage?: Storage): Record<string, string[]> {
  if (!storage) return {};
  try {
    const parsed: unknown = JSON.parse(storage.getItem(STORAGE_KEY) ?? "{}");
    if (typeof parsed !== "object" || parsed === null) return {};
    return Object.fromEntries(
      Object.entries(parsed).map(([projectId, paths]) => [
        projectId,
        Array.isArray(paths) ? paths.filter(
          (path): path is string => typeof path === "string" && isRelativeServerPath(path),
        ) : [],
      ]),
    );
  } catch {
    return {};
  }
}

export interface FileTreeStoreState {
  readonly expanded: (projectId: string) => readonly string[];
  readonly expandedByProject: Readonly<Record<string, readonly string[]>>;
  readonly setExpanded: (projectId: string, paths: readonly string[]) => void;
}

export function createFileTreeStore(storage?: Storage) {
  return createStore<FileTreeStoreState>()((set, get) => ({
    expanded: (projectId) => get().expandedByProject[projectId] ?? [],
    expandedByProject: restore(storage),
    setExpanded: (projectId, paths) => {
      const safePaths = [...new Set(paths.filter(isRelativeServerPath))];
      set((state) => {
        const expandedByProject = { ...state.expandedByProject, [projectId]: safePaths };
        try {
          storage?.setItem(STORAGE_KEY, JSON.stringify(expandedByProject));
        } catch {
          // Tree expansion is optional UI state; storage failures must not block the workspace.
        }
        return { expandedByProject };
      });
    },
  }));
}
