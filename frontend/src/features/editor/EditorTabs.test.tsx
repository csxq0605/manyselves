import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { createEditorStore } from "./editor-store";
import { EditorTabs } from "./EditorTabs";

const serverFile = {
  content: "old",
  modifiedAt: "2026-08-02T00:00:00Z",
  path: "Inputs/a.md",
  revision: "a".repeat(64),
  size: 3,
};

describe("editor store", () => {
  it("keeps a dirty draft when the server revision changes", () => {
    const store = createEditorStore();
    store.getState().openFile("project-1", serverFile);
    store.getState().updateDraft(serverFile.path, "old local");
    store.getState().markServerChanged(serverFile.path, "b".repeat(64));

    expect(store.getState().tabs[0]).toEqual(expect.objectContaining({
      dirty: true,
      draftContent: "old local",
      externalRevision: "b".repeat(64),
      serverRevision: "a".repeat(64),
    }));
  });

  it("does not replace newer typing with an older save response", () => {
    const store = createEditorStore();
    store.getState().openFile("project-1", serverFile);
    store.getState().updateDraft(serverFile.path, "newer typing");
    store.getState().applySaved({
      ...serverFile,
      content: "content sent earlier",
      revision: "c".repeat(64),
    });

    expect(store.getState().tabs[0]).toEqual(expect.objectContaining({
      dirty: true,
      draftContent: "newer typing",
      serverContent: "content sent earlier",
      serverRevision: "c".repeat(64),
    }));
  });
});

describe("EditorTabs", () => {
  it("confirms before closing a dirty tab and preserves it when declined", async () => {
    const store = createEditorStore();
    store.getState().openFile("project-1", serverFile);
    store.getState().updateDraft(serverFile.path, "changed");
    const confirmClose = vi.fn(() => false);
    const user = userEvent.setup();

    render(<EditorTabs confirmClose={confirmClose} store={store} />);
    await user.click(screen.getByRole("button", { name: "关闭 Inputs/a.md" }));

    expect(confirmClose).toHaveBeenCalledOnce();
    expect(screen.getByRole("tab", { name: /a.md/ })).toBeVisible();
  });
});
