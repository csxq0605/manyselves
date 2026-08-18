import { describe, expect, it } from "vitest";

import { createFileTreeStore } from "./file-tree-store";

describe("file tree store", () => {
  it("persists only server-relative expanded paths per project", () => {
    const values = new Map<string, string>();
    const storage: Storage = {
      clear: () => values.clear(),
      getItem: (key) => values.get(key) ?? null,
      key: (index) => [...values.keys()][index] ?? null,
      get length() { return values.size; },
      removeItem: (key) => { values.delete(key); },
      setItem: (key, value) => { values.set(key, value); },
    };
    const first = createFileTreeStore(storage);
    first.getState().setExpanded("p1", ["Inputs", "/absolute", "../escape"]);

    const restored = createFileTreeStore(storage);
    expect(restored.getState().expanded("p1")).toEqual(["Inputs"]);
    expect(restored.getState().expanded("p2")).toEqual([]);
  });
});
