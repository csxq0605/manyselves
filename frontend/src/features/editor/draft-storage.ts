import { type DBSchema, openDB } from "idb";

export interface StoredDraft {
  readonly content: string;
  readonly key: string;
  readonly path: string;
  readonly projectId: string;
  readonly serverRevision: string;
  readonly serverUrl: string;
  readonly updatedAt: number;
}

interface DraftDatabase extends DBSchema {
  drafts: {
    key: string;
    value: StoredDraft;
  };
}

export function draftKey(serverUrl: string, projectId: string, path: string): string {
  return JSON.stringify([serverUrl.replace(/\/$/, ""), projectId, path]);
}

export function pruneDrafts(drafts: readonly StoredDraft[], maximum: number): StoredDraft[] {
  return [...drafts].sort((left, right) => right.updatedAt - left.updatedAt).slice(0, maximum);
}

interface TabStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function tabListKey(serverUrl: string, projectId: string): string {
  return `manyselves.editorTabs.v1:${draftKey(serverUrl, projectId, "")}`;
}

function isRelativePath(path: string): boolean {
  return path.length > 0 && !path.startsWith("/") && !path.startsWith("\\") &&
    path.split(/[\\/]/).every((part) => part !== ".." && part !== "");
}

export function loadOpenTabPaths(
  storage: TabStorage,
  serverUrl: string,
  projectId: string,
): string[] {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(tabListKey(serverUrl, projectId)) ?? "[]");
    return Array.isArray(parsed)
      ? parsed.filter((path): path is string => typeof path === "string" && isRelativePath(path))
      : [];
  } catch {
    return [];
  }
}

export function saveOpenTabPaths(
  storage: Pick<TabStorage, "setItem">,
  serverUrl: string,
  projectId: string,
  paths: readonly string[],
): void {
  try {
    storage.setItem(
      tabListKey(serverUrl, projectId),
      JSON.stringify([...new Set(paths.filter(isRelativePath))].slice(0, 30)),
    );
  } catch {
    // Open-tab restoration is optional UI state.
  }
}

export interface DraftRepository {
  list(serverUrl: string, projectId: string): Promise<StoredDraft[]>;
  remove(serverUrl: string, projectId: string, path: string): Promise<void>;
  save(draft: Omit<StoredDraft, "key">): Promise<void>;
}

export function createIndexedDbDraftRepository(maximumDrafts = 50): DraftRepository {
  const database = openDB<DraftDatabase>("manyselves-drafts", 1, {
    upgrade(db) {
      db.createObjectStore("drafts", { keyPath: "key" });
    },
  });

  return {
    async list(serverUrl, projectId) {
      const all = await (await database).getAll("drafts");
      return pruneDrafts(
        all.filter((draft) => draft.serverUrl === serverUrl && draft.projectId === projectId),
        maximumDrafts,
      );
    },
    async remove(serverUrl, projectId, path) {
      await (await database).delete("drafts", draftKey(serverUrl, projectId, path));
    },
    async save(draft) {
      const db = await database;
      const saved = { ...draft, key: draftKey(draft.serverUrl, draft.projectId, draft.path) };
      await db.put("drafts", saved);
      const all = await db.getAll("drafts");
      const retained = new Set(pruneDrafts(all, maximumDrafts).map((item) => item.key));
      await Promise.all(all.filter((item) => !retained.has(item.key)).map((item) => db.delete("drafts", item.key)));
    },
  };
}
