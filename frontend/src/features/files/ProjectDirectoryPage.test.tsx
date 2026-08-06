import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiGateway } from "../../api/gateway";
import { AppProviders } from "../../app/providers";
import type { PlatformBridge } from "../../platform/types";
import type { FileApi, FileEntry } from "./file-api";
import { ProjectDirectoryPage } from "./ProjectDirectoryPage";
import { SECTION_CAPABILITIES } from "./project-sections";
import { UploadConflictDialog } from "./UploadConflictDialog";

const entries: FileEntry[] = [
  {
    kind: "directory",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: "Reports",
    path: "Outputs/Reports",
    revision: "a".repeat(64),
    size: null,
  },
  {
    kind: "file",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: "summary.md",
    path: "Outputs/Reports/summary.md",
    revision: "b".repeat(64),
    size: 12,
  },
  {
    kind: "directory",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: "Modules",
    path: "Outputs/Modules",
    revision: "d".repeat(64),
    size: null,
  },
  {
    kind: "directory",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: "Reviews",
    path: "Outputs/Reviews",
    revision: "e".repeat(64),
    size: null,
  },
];

const paginatedOutputEntries: FileEntry[] = [
  {
    kind: "directory",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: "Modules",
    path: "Outputs/Modules",
    revision: "d".repeat(64),
    size: null,
  },
  ...Array.from({ length: 21 }, (_, index): FileEntry => ({
    kind: "file",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: `module-${String(index + 1).padStart(2, "0")}.md`,
    path: `Outputs/Modules/module-${String(index + 1).padStart(2, "0")}.md`,
    revision: `${index}`.padStart(64, "0"),
    size: index + 1,
  })),
  {
    kind: "directory",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: "Reports",
    path: "Outputs/Reports",
    revision: "a".repeat(64),
    size: null,
  },
  {
    kind: "file",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: "summary.md",
    path: "Outputs/Reports/summary.md",
    revision: "b".repeat(64),
    size: 12,
  },
  {
    kind: "directory",
    modifiedAt: "2026-08-04T00:00:00Z",
    name: "Reviews",
    path: "Outputs/Reviews",
    revision: "e".repeat(64),
    size: null,
  },
];

const paginatedInputEntries: FileEntry[] = Array.from({ length: 21 }, (_, index): FileEntry => ({
  kind: "file",
  modifiedAt: "2026-08-04T00:00:00Z",
  name: `input-${String(index + 1).padStart(2, "0")}.md`,
  path: `Inputs/input-${String(index + 1).padStart(2, "0")}.md`,
  revision: `${index}`.padStart(64, "1"),
  size: index + 1,
}));

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

function fileApi(overrides: Partial<FileApi> = {}): FileApi {
  return {
    createEntry: async () => entries[1]!,
    deleteEntry: async () => undefined,
    download: async () => new Blob(["result"]),
    listTree: async () => [],
    renameEntry: async () => entries[1]!,
    upload: async () => entries[1]!,
    ...overrides,
  };
}

function renderDirectory(
  section: "inputs" | "knowledge" | "templates" | "outputs",
  api: FileApi,
  bridge = platform(),
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const gateway = { baseUrl: "https://api.example" } as unknown as ApiGateway;
  return render(
    <AppProviders queryClient={queryClient}>
      <MemoryRouter initialEntries={[`/projects/project-1/${section}`]}>
        <Routes>
          <Route
            element={<ProjectDirectoryPage api={api} gateway={gateway} platform={bridge} />}
            path="/projects/:projectId/:section"
          />
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
}

describe("section capabilities", () => {
  it("keeps templates and outputs separate and makes outputs read-only except delete", () => {
    expect(SECTION_CAPABILITIES.templates.root).toBe("Templates");
    expect(SECTION_CAPABILITIES.outputs).toMatchObject({
      delete: true,
      download: true,
      edit: false,
      preview: true,
      root: "Outputs",
      upload: false,
    });
  });
});

describe("ProjectDirectoryPage", () => {
  it("renders outputs as paginated tabs inside a bounded scroll panel", async () => {
    const user = userEvent.setup();
    renderDirectory("outputs", fileApi({ listTree: async () => paginatedOutputEntries }));

    expect(await screen.findByRole("tab", { name: /Modules\s*21/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Reports\s*1/ })).toBeVisible();
    expect(screen.getByRole("tab", { name: /Reviews\s*0/ })).toBeVisible();
    expect(screen.getByRole("region", { name: "Modules output scroll area" })).toHaveClass("output-tabs__viewport");
    expect(screen.getByText("module-01.md")).toBeVisible();
    expect(screen.queryByText("module-21.md")).not.toBeInTheDocument();
    expect(screen.getByText("Page 1 / 2")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("module-21.md")).toBeVisible();
    expect(screen.queryByText("module-01.md")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Reports\s*1/ }));
    expect(screen.getByText("summary.md")).toBeVisible();
  });

  it("never offers upload or edit on outputs", async () => {
    const user = userEvent.setup();
    const { container } = renderDirectory("outputs", fileApi({ listTree: async () => entries }));

    await user.click(await screen.findByRole("tab", { name: /Reports\s*1/ }));
    const row = (await screen.findByText("summary.md")).closest("li");
    expect(row?.querySelectorAll("button")).toHaveLength(3);
    expect(container.querySelector('input[type="file"]')).not.toBeInTheDocument();
  });

  it("paginates regular file sections inside a bounded scroll panel", async () => {
    const user = userEvent.setup();
    renderDirectory("inputs", fileApi({ listTree: async () => paginatedInputEntries }));

    expect(await screen.findByRole("region", { name: "Inputs file scroll area" })).toHaveClass("file-list__viewport");
    expect(screen.getByText("input-01.md")).toBeVisible();
    expect(screen.queryByText("input-21.md")).not.toBeInTheDocument();
    expect(screen.getByText("Page 1 / 2")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("input-21.md")).toBeVisible();
    expect(screen.queryByText("input-01.md")).not.toBeInTheDocument();
  });

  it("uploads browser File objects from a hidden multiple file input", async () => {
    const upload = vi.fn().mockResolvedValue({
      ...entries[1]!,
      name: "brief.txt",
      path: "Inputs/brief.txt",
    });
    const user = userEvent.setup();
    renderDirectory("inputs", fileApi({ upload }));
    const file = new File(["brief"], "brief.txt", { type: "text/plain" });

    await user.click(await screen.findByRole("button", { name: "上传本地文件" }));
    await user.upload(screen.getByLabelText("选择本地文件"), file);

    await waitFor(() => expect(upload).toHaveBeenCalledWith(
      "project-1",
      "Inputs/brief.txt",
      file,
      "reject",
      undefined,
      expect.any(AbortSignal),
    ));
    expect(screen.getByLabelText("选择本地文件")).toHaveAttribute("multiple");
  });

  it("uses the selected server revision when replacing a duplicate upload", async () => {
    const existing: FileEntry = {
      ...entries[1]!,
      name: "brief.txt",
      path: "Inputs/brief.txt",
      revision: "c".repeat(64),
    };
    const upload = vi.fn()
      .mockRejectedValueOnce(new ApiError({
        code: "FILE_ALREADY_EXISTS",
        details: { path: "Inputs/brief.txt" },
        message: "already exists",
        requestId: "request-1",
        retryable: false,
        status: 409,
      }))
      .mockResolvedValue(existing);
    const user = userEvent.setup();
    renderDirectory("inputs", fileApi({ listTree: async () => [existing], upload }));
    const file = new File(["new"], "brief.txt", { type: "text/plain" });

    await user.upload(await screen.findByLabelText("选择本地文件"), file);
    expect(await screen.findByRole("dialog", { name: "文件已存在" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "替换" }));

    await waitFor(() => expect(upload).toHaveBeenLastCalledWith(
      "project-1",
      "Inputs/brief.txt",
      file,
      "replace",
      existing.revision,
      expect.any(AbortSignal),
    ));
  });

  it("refetches a duplicate that was absent from the cached directory before replacing", async () => {
    const existing: FileEntry = {
      ...entries[1]!,
      name: "fresh.txt",
      path: "Inputs/fresh.txt",
      revision: "f".repeat(64),
    };
    const listTree = vi.fn().mockResolvedValueOnce([]).mockResolvedValue([existing]);
    const upload = vi.fn()
      .mockRejectedValueOnce(new ApiError({
        code: "FILE_ALREADY_EXISTS",
        details: { path: existing.path },
        message: "already exists",
        requestId: "request-2",
        retryable: false,
        status: 409,
      }))
      .mockResolvedValue(existing);
    const user = userEvent.setup();
    renderDirectory("inputs", fileApi({ listTree, upload }));
    const file = new File(["new"], existing.name, { type: "text/plain" });

    await user.upload(await screen.findByLabelText("选择本地文件"), file);
    expect(await screen.findByRole("dialog", { name: "文件已存在" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "替换" }));

    await waitFor(() => expect(upload).toHaveBeenLastCalledWith(
      "project-1",
      existing.path,
      file,
      "replace",
      existing.revision,
      expect.any(AbortSignal),
    ));
    expect(listTree).toHaveBeenCalledTimes(3);
  });

  it("waits for one file conflict decision before uploading the next selected file", async () => {
    const existing: FileEntry = {
      ...entries[1]!,
      name: "first.txt",
      path: "Inputs/first.txt",
      revision: "1".repeat(64),
    };
    const upload = vi.fn(async (_projectId: string, path: string, _file: File, conflict: string) => {
      if (path === existing.path && conflict === "reject") {
        throw new ApiError({
          code: "FILE_ALREADY_EXISTS",
          details: { path },
          message: "already exists",
          requestId: "request-3",
          retryable: false,
          status: 409,
        });
      }
      return { ...existing, name: path.split("/").at(-1) ?? path, path };
    });
    const user = userEvent.setup();
    renderDirectory("inputs", fileApi({ listTree: async () => [existing], upload }));
    const first = new File(["one"], "first.txt", { type: "text/plain" });
    const second = new File(["two"], "second.txt", { type: "text/plain" });

    await user.upload(await screen.findByLabelText("选择本地文件"), [first, second]);
    expect(await screen.findByRole("dialog", { name: "文件已存在" })).toBeVisible();
    expect(upload).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "保留两份" }));
    await waitFor(() => expect(upload).toHaveBeenCalledWith(
      "project-1",
      "Inputs/second.txt",
      second,
      "reject",
      undefined,
      expect.any(AbortSignal),
    ));
  });

  it("cancels a pending upload queue when the routed project changes", async () => {
    const existing: FileEntry = {
      ...entries[1]!,
      name: "first.txt",
      path: "Inputs/first.txt",
      revision: "f".repeat(64),
    };
    const upload = vi.fn(async (_projectId: string, path: string, _file: File, mode: string) => {
      if (path === existing.path && mode === "reject") {
        throw new ApiError({
          code: "FILE_ALREADY_EXISTS",
          details: { path },
          message: "already exists",
          requestId: "request-switch",
          retryable: false,
          status: 409,
        });
      }
      return existing;
    });
    const api = fileApi({ listTree: async () => [existing], upload });
    const gateway = { baseUrl: "https://api.example" } as unknown as ApiGateway;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <AppProviders queryClient={queryClient}>
        <MemoryRouter initialEntries={["/projects/project-1/inputs"]}>
          <Link to="/projects/project-2/inputs">切换项目</Link>
          <Routes>
            <Route
              element={<ProjectDirectoryPage api={api} gateway={gateway} platform={platform()} />}
              path="/projects/:projectId/:section"
            />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    const first = new File(["one"], "first.txt", { type: "text/plain" });
    const second = new File(["two"], "second.txt", { type: "text/plain" });

    await user.upload(await screen.findByLabelText("选择本地文件"), [first, second]);
    expect(await screen.findByRole("dialog", { name: "文件已存在" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "切换项目" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "文件已存在" })).not.toBeInTheDocument());
    expect(upload).toHaveBeenCalledTimes(1);
  });

  it("aborts an active upload when the routed project changes", async () => {
    let activeSignal: AbortSignal | undefined;
    const upload = vi.fn((
      _projectId: string,
      _path: string,
      _file: File,
      _mode: string,
      _baseRevision?: string,
      signal?: AbortSignal,
    ) => {
      activeSignal = signal;
      return new Promise<FileEntry>((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    });
    const api = fileApi({ upload });
    const gateway = { baseUrl: "https://api.example" } as unknown as ApiGateway;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <AppProviders queryClient={queryClient}>
        <MemoryRouter initialEntries={["/projects/project-1/inputs"]}>
          <Link to="/projects/project-2/inputs">切换项目</Link>
          <Routes>
            <Route
              element={<ProjectDirectoryPage api={api} gateway={gateway} platform={platform()} />}
              path="/projects/:projectId/:section"
            />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    await user.upload(
      await screen.findByLabelText("选择本地文件"),
      new File(["one"], "active.txt", { type: "text/plain" }),
    );
    await waitFor(() => expect(activeSignal).toBeDefined());
    await user.click(screen.getByRole("link", { name: "切换项目" }));

    expect(activeSignal?.aborted).toBe(true);
  });

  it("renders loading, empty, error, and refresh states", async () => {
    let resolveEntries!: (value: FileEntry[]) => void;
    const listTree = vi.fn()
      .mockImplementationOnce(() => new Promise<FileEntry[]>((resolve) => { resolveEntries = resolve; }))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue([]);
    const user = userEvent.setup();
    renderDirectory("knowledge", fileApi({ listTree }));

    expect(screen.getByRole("status")).toHaveTextContent("正在加载文件");
    resolveEntries([]);
    expect(await screen.findByText("此目录还没有文件")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("文件列表加载失败");
    await user.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByText("此目录还没有文件")).toBeVisible();
    expect(listTree).toHaveBeenCalledTimes(3);
  });
});

describe("UploadConflictDialog", () => {
  it("offers replace, keep both, and cancel decisions", async () => {
    const onResolve = vi.fn();
    const user = userEvent.setup();
    render(
      <UploadConflictDialog
        fileName="brief.txt"
        onResolve={onResolve}
      />,
    );

    await user.click(screen.getByRole("button", { name: "替换" }));
    await user.click(screen.getByRole("button", { name: "保留两份" }));
    await user.click(screen.getByRole("button", { name: "取消" }));

    expect(onResolve.mock.calls).toEqual([["replace"], ["keep-both"], ["cancel"]]);
  });

  it("keeps keyboard focus inside and closes on Escape", async () => {
    const onResolve = vi.fn();
    const user = userEvent.setup();
    render(<UploadConflictDialog fileName="brief.txt" onResolve={onResolve} />);
    const replace = screen.getByRole("button", { name: "替换" });
    const cancel = screen.getByRole("button", { name: "取消" });

    await waitFor(() => expect(replace).toHaveFocus());
    cancel.focus();
    await user.tab();
    expect(replace).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(onResolve).toHaveBeenLastCalledWith("cancel");
  });
});
