import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentType } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/gateway";
import { createEditorStore } from "./editor-store";
import type { EditorFileApi } from "./editor-api";
import { EditorWorkspace } from "./EditorWorkspace";
import type { TextEditorProps } from "./TextEditor";

const first = {
  content: "server one", modifiedAt: "2026-08-02T00:00:00Z", path: "Inputs/a.md",
  revision: "a".repeat(64), size: 10,
};
const latest = { ...first, content: "server two", revision: "b".repeat(64) };
const saved = { ...first, content: "local draft", revision: "c".repeat(64) };

const TestEditor: ComponentType<TextEditorProps> = ({ onChange, tab }) => (
  <textarea aria-label="代码编辑器" onChange={(event) => onChange(event.target.value)} value={tab.draftContent} />
);

describe("EditorWorkspace", () => {
  beforeEach(() => window.localStorage.clear());

  it("uses the latest revision only after a second explicit overwrite confirmation", async () => {
    const store = createEditorStore();
    store.getState().openFile("p1", first);
    const revisions: string[] = [];
    const api: EditorFileApi = {
      read: async () => latest,
      save: async (_projectId, _path, content, baseRevision) => {
        revisions.push(baseRevision);
        if (revisions.length === 1) {
          throw new ApiError({
            code: "FILE_REVISION_CONFLICT", details: {}, message: "conflict",
            requestId: "request-1", retryable: false, status: 409,
          });
        }
        return { ...saved, content };
      },
    };
    const repository = {
      list: async () => [], remove: async () => undefined, save: async () => undefined,
    };
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();

    render(
      <EditorWorkspace
        api={api}
        editorComponent={TestEditor}
        projectId="p1"
        repository={repository}
        serverUrl="https://server"
        store={store}
      />,
    );
    await user.clear(screen.getByRole("textbox", { name: "代码编辑器" }));
    await user.type(screen.getByRole("textbox", { name: "代码编辑器" }), "local draft");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("服务器文件已更新");

    await user.click(screen.getByRole("button", { name: "覆盖服务器版本" }));

    expect(revisions).toEqual([first.revision, latest.revision]);
    expect(store.getState().tabs[0]).toEqual(expect.objectContaining({
      dirty: false,
      serverRevision: saved.revision,
    }));
  });

  it("restores a persisted dirty draft without replacing it with server content", async () => {
    const store = createEditorStore();
    const api: EditorFileApi = {
      read: async () => latest,
      save: async () => saved,
    };
    const repository = {
      list: async () => [{
        content: "recovered local", key: "draft", path: first.path, projectId: "p1",
        serverRevision: first.revision, serverUrl: "https://server", updatedAt: 1,
      }],
      remove: async () => undefined,
      save: async () => undefined,
    };

    render(
      <EditorWorkspace
        api={api}
        editorComponent={TestEditor}
        projectId="p1"
        repository={repository}
        serverUrl="https://server"
        store={store}
      />,
    );

    expect(await screen.findByRole("textbox", { name: "代码编辑器" })).toHaveValue("recovered local");
    expect(screen.getByRole("status")).toHaveTextContent("服务器文件已更新");
  });
});
