import { describe, expect, it } from "vitest";

import {
  draftKey,
  loadOpenTabPaths,
  pruneDrafts,
  saveOpenTabPaths,
  type StoredDraft,
} from "./draft-storage";

describe("draft storage", () => {
  it("scopes drafts by server, project, and relative path", () => {
    expect(draftKey("https://server", "p1", "Inputs/a.md")).not.toBe(
      draftKey("https://server", "p2", "Inputs/a.md"),
    );
  });

  it("keeps the newest bounded drafts", () => {
    const drafts: StoredDraft[] = [1, 3, 2].map((updatedAt) => ({
      content: String(updatedAt), key: String(updatedAt), path: `${updatedAt}.md`,
      projectId: "p1", serverRevision: "a".repeat(64), serverUrl: "https://server", updatedAt,
    }));
    expect(pruneDrafts(drafts, 2).map((draft) => draft.updatedAt)).toEqual([3, 2]);
  });

  it("restores only relative tab paths for the current server and project", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
    };
    saveOpenTabPaths(storage, "https://server", "p1", ["Inputs/a.md", "/outside"]);

    expect(loadOpenTabPaths(storage, "https://server", "p1")).toEqual(["Inputs/a.md"]);
    expect(loadOpenTabPaths(storage, "https://server", "p2")).toEqual([]);
  });
});
