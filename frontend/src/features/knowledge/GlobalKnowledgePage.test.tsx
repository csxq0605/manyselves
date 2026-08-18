import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { AppProviders } from "../../app/providers";
import type { PlatformBridge } from "../../platform/types";
import type { GlobalKnowledgeApi, GlobalKnowledgeEntry } from "./global-knowledge-api";
import { GlobalKnowledgePage } from "./GlobalKnowledgePage";

vi.mock("../editor/EditorWorkspace", () => ({
  EditorWorkspace: () => <div aria-label="测试编辑器" />,
}));

const entry: GlobalKnowledgeEntry = {
  kind: "file",
  modifiedAt: "2026-08-04T00:00:00Z",
  name: "standard.md",
  path: "standard.md",
  revision: "a".repeat(64),
  size: 12,
};

function api(overrides: Partial<GlobalKnowledgeApi> = {}): GlobalKnowledgeApi {
  return {
    deleteEntry: async () => undefined,
    download: async () => new Blob(["standard"]),
    downloadRange: async () => new Uint8Array(),
    listTree: async () => [entry],
    preview: async () => ({ content: "# Standard", kind: "markdown", path: entry.path, truncated: false }),
    read: async (path) => ({ content: "# Standard", modifiedAt: entry.modifiedAt, path, revision: entry.revision, size: 10 }),
    save: async (path, content) => ({ content, modifiedAt: entry.modifiedAt, path, revision: entry.revision, size: content.length }),
    upload: async () => entry,
    ...overrides,
  };
}

function platform(): PlatformBridge {
  return {
    kind: "browser",
    notify: async () => undefined,
    openDownloadedFile: async () => undefined,
    saveDownload: async () => undefined,
    selectDirectory: async () => null,
    selectFiles: async () => [],
  };
}

function renderPage(resolvedApi = api(), bridge = platform()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const gateway = { baseUrl: "https://api.example" } as ApiGateway;
  return render(
    <AppProviders queryClient={queryClient}>
      <GlobalKnowledgePage api={resolvedApi} gateway={gateway} platform={bridge} />
    </AppProviders>,
  );
}

describe("GlobalKnowledgePage", () => {
  it("uses browser upload and never renders a server path picker", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "全局知识库" })).toBeVisible();
    expect(screen.getByRole("button", { name: "上传文件" })).toBeVisible();
    expect(screen.queryByText("服务器文件")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(".manyselves");
  });

  it("uploads a browser File to a logical root path", async () => {
    const upload = vi.fn().mockResolvedValue(entry);
    const user = userEvent.setup();
    renderPage(api({ listTree: async () => [], upload }));
    const file = new File(["shared"], "shared.md", { type: "text/markdown" });

    await user.upload(await screen.findByLabelText("选择本地文件"), file);

    await waitFor(() => expect(upload).toHaveBeenCalledWith(
      "shared.md",
      file,
      "reject",
      undefined,
      expect.any(AbortSignal),
    ));
  });

  it("uses the latest logical entry revision when replacing a duplicate", async () => {
    const upload = vi.fn()
      .mockRejectedValueOnce(Object.assign(new Error("already exists"), { code: "FILE_ALREADY_EXISTS" }))
      .mockResolvedValue(entry);
    const user = userEvent.setup();
    renderPage(api({ upload }));

    await user.upload(await screen.findByLabelText("选择本地文件"), new File(["new"], entry.name));
    await user.click(await screen.findByRole("button", { name: "替换" }));

    await waitFor(() => expect(upload).toHaveBeenLastCalledWith(
      entry.path,
      expect.any(File),
      "replace",
      entry.revision,
      expect.any(AbortSignal),
    ));
  });

  it("keeps file operations on file rows and directories non-operable", async () => {
    renderPage(api({ listTree: async () => [
      { ...entry, kind: "directory", name: "rules", path: "rules", size: null },
      { ...entry, path: "rules/standard.md" },
    ] }));

    const directory = await screen.findByRole("button", { name: "展开 rules" });
    expect(screen.queryByRole("group", { name: "rules 文件操作" })).not.toBeInTheDocument();
    await userEvent.click(directory);
    expect(screen.getByRole("button", { name: "编辑 standard.md" })).toBeVisible();
    expect(screen.getByRole("button", { name: "预览 standard.md" })).toBeVisible();
    expect(screen.getByRole("button", { name: "下载 standard.md" })).toBeVisible();
    expect(screen.getByRole("button", { name: "删除 standard.md" })).toBeVisible();
  });

  it("downloads, deletes, and opens editable text through shared file capabilities", async () => {
    const deleteEntry = vi.fn().mockResolvedValue(undefined);
    const download = vi.fn().mockResolvedValue(new Blob(["standard"]));
    const read = vi.fn().mockResolvedValue({
      content: "# Standard", modifiedAt: entry.modifiedAt, path: entry.path,
      revision: entry.revision, size: 10,
    });
    const saveDownload = vi.fn().mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderPage(api({ deleteEntry, download, read }), { ...platform(), saveDownload });

    await user.click(await screen.findByRole("button", { name: "下载 standard.md" }));
    await waitFor(() => expect(saveDownload).toHaveBeenCalledWith({
      blob: expect.any(Blob), suggestedName: entry.name,
    }));
    await user.click(screen.getByRole("button", { name: "删除 standard.md" }));
    await waitFor(() => expect(deleteEntry).toHaveBeenCalledWith(entry.path, entry.revision));
    await user.click(screen.getByRole("button", { name: "编辑 standard.md" }));
    await waitFor(() => expect(read).toHaveBeenCalledWith(entry.path));
    expect(await screen.findByRole("button", { name: "关闭编辑器" })).toBeVisible();
    confirm.mockRestore();
  });

  it("reports preview parsing failure while retaining the uploaded file", async () => {
    const deleteEntry = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage(api({ deleteEntry, preview: async () => { throw new Error("parse failed"); } }));

    await user.click(await screen.findByRole("button", { name: "预览 standard.md" }));

    expect(await screen.findByText("文件预览或解析失败，原文件已保留")).toBeVisible();
    expect(deleteEntry).not.toHaveBeenCalled();
  });
});
