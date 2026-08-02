import { describe, expect, it } from "vitest";

import { buildSelectionContext } from "./selection-context";

describe("selection context", () => {
  it("sends only the active file selection metadata", () => {
    expect(buildSelectionContext({
      endLine: 4,
      path: "Inputs/a.py",
      startLine: 2,
    })).toEqual({
      endLine: 4,
      file: "Inputs/a.py",
      startLine: 2,
      type: "selection",
    });
  });
});
