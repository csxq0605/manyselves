import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PlatformBridge } from "../../platform/types";
import { FileActions } from "./FileActions";
import type { FileApi, FileEntry } from "./file-api";
import { FileTree } from "./FileTree";

const entries: FileEntry[] = [
  {
    kind: "directory",
    modifiedAt: "2026-08-02T00:00:00Z",
    name: "Inputs",
    path: "Inputs",
    revision: "a".repeat(64),
    size: null,
  },
  {
    kind: "file",
    modifiedAt: "2026-08-02T00:00:00Z",
    name: "a.md",
    path: "Inputs/a.md",
    revision: "b".repeat(64),
    size: 4,
  },
];

describe("FileTree", () => {
  it("opens, renames, and deletes a file using keyboard tree semantics", async () => {
    const effects: string[] = [];
    const user = userEvent.setup();

    render(
      <FileTree
        confirmDelete={() => true}
        entries={entries}
        initialExpanded={["Inputs"]}
        onDelete={(entry) => effects.push(`delete:${entry.path}`)}
        onOpen={(entry) => effects.push(`open:${entry.path}`)}
        onRename={(entry) => effects.push(`rename:${entry.path}`)}
        projectId="project-1"
      />,
    );

    const file = screen.getByRole("treeitem", { name: "a.md" });
    file.focus();
    await user.keyboard("{Enter}{F2}{Delete}");

    expect(screen.getByRole("tree", { name: "项目文件" })).toBeVisible();
    expect(effects).toEqual([
      "open:Inputs/a.md",
      "rename:Inputs/a.md",
      "delete:Inputs/a.md",
    ]);
  });

  it("expands directories and moves focus with arrow keys", async () => {
    const user = userEvent.setup();
    render(
      <FileTree
        confirmDelete={() => true}
        entries={entries}
        onDelete={() => undefined}
        onOpen={() => undefined}
        onRename={() => undefined}
        projectId="project-1"
      />,
    );

    const directory = screen.getByRole("treeitem", { name: "Inputs" });
    directory.focus();
    await user.keyboard("{ArrowRight}{ArrowDown}");

    expect(screen.getByRole("treeitem", { name: "a.md" })).toHaveFocus();
  });
});

describe("FileActions", () => {
  it("imports selected files into the chosen server directory", async () => {
    const file = new File(["x"], "a.csv", { type: "text/csv" });
    const uploads: string[] = [];
    const platform: PlatformBridge = {
      kind: "browser",
      notify: async () => undefined,
      openDownloadedFile: async () => undefined,
      saveDownload: async () => undefined,
      selectDirectory: async () => null,
      selectFiles: async () => [
        { file, name: file.name, size: file.size, type: file.type },
      ],
    };
    const api: FileApi = {
      createEntry: async () => entries[0]!,
      deleteEntry: async () => undefined,
      download: async () => new Blob(),
      listTree: async () => entries,
      renameEntry: async () => entries[0]!,
      upload: async (_projectId, path) => {
        uploads.push(path);
        return entries[1]!;
      },
    };
    const user = userEvent.setup();

    render(
      <FileActions
        api={api}
        directory="Inputs"
        platform={platform}
        projectId="project-1"
      />,
    );

    await user.click(screen.getByRole("button", { name: "导入文件" }));

    expect(uploads).toEqual(["Inputs/a.csv"]);
    expect(await screen.findByText("已完成")).toBeVisible();
  });

  it("uses server revisions for rename and delete and downloads through the platform", async () => {
    const calls: string[] = [];
    const platform: PlatformBridge = {
      kind: "browser",
      notify: async () => undefined,
      openDownloadedFile: async () => undefined,
      saveDownload: async ({ suggestedName }) => { calls.push(`download:${suggestedName}`); },
      selectDirectory: async () => null,
      selectFiles: async () => [],
    };
    const api: FileApi = {
      createEntry: async (_projectId, input) => {
        calls.push(`create:${input.kind}:${input.path}`);
        return entries[1]!;
      },
      deleteEntry: async (_projectId, path, revision) => { calls.push(`delete:${path}:${revision}`); },
      download: async (_projectId, path) => {
        calls.push(`fetch:${path}`);
        return new Blob(["x"]);
      },
      listTree: async () => entries,
      renameEntry: async (_projectId, input) => {
        calls.push(`rename:${input.source}:${input.destination}:${input.baseRevision}`);
        return entries[1]!;
      },
      upload: async () => entries[1]!,
    };
    const names = ["new.md", "renamed.md"];
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(
      <FileActions
        api={api}
        directory="Inputs"
        platform={platform}
        projectId="project-1"
        requestName={() => names.shift() ?? null}
        selectedEntry={entries[1]!}
      />,
    );

    await user.click(screen.getByRole("button", { name: "新建文件" }));
    await user.click(screen.getByRole("button", { name: "重命名" }));
    await user.click(screen.getByRole("button", { name: "删除" }));
    await user.click(screen.getByRole("button", { name: "下载" }));

    expect(calls).toEqual([
      "create:file:Inputs/new.md",
      `rename:Inputs/a.md:Inputs/renamed.md:${"b".repeat(64)}`,
      `delete:Inputs/a.md:${"b".repeat(64)}`,
      "fetch:Inputs/a.md",
      "download:a.md",
    ]);
  });

  it("shows a destination summary before importing a local directory", async () => {
    const file = new File(["x"], "meter.csv");
    const uploads: string[] = [];
    const platform: PlatformBridge = {
      kind: "browser",
      notify: async () => undefined,
      openDownloadedFile: async () => undefined,
      saveDownload: async () => undefined,
      selectDirectory: async () => ({
        files: [{ file, name: file.name, size: file.size, type: file.type }],
        name: "August",
      }),
      selectFiles: async () => [],
    };
    const api = {
      createEntry: async () => entries[0]!, deleteEntry: async () => undefined,
      download: async () => new Blob(), listTree: async () => entries,
      renameEntry: async () => entries[0]!,
      upload: async (_projectId: string, path: string) => {
        uploads.push(path);
        return entries[1]!;
      },
    } satisfies FileApi;
    const user = userEvent.setup();

    render(<FileActions api={api} directory="Inputs" platform={platform} projectId="p1" />);
    await user.click(screen.getByRole("button", { name: "导入目录" }));
    expect(screen.getByText("1 个文件将导入到 Inputs/August")).toBeVisible();
    expect(uploads).toEqual([]);

    await user.click(screen.getByRole("button", { name: "开始导入" }));
    expect(uploads).toEqual(["Inputs/August/meter.csv"]);
  });
});
