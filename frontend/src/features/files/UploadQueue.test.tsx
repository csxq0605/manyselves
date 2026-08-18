import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { PlatformBridge } from "../../platform/types";
import type { FileApi } from "./file-api";
import { FileActions } from "./FileActions";

describe("UploadQueue", () => {
  it("keeps a failed upload out of completed state and retries explicitly", async () => {
    const file = new File(["x"], "meter.csv");
    let attempts = 0;
    const api: FileApi = {
      createEntry: async () => { throw new Error("unused"); },
      deleteEntry: async () => undefined,
      download: async () => new Blob(),
      listTree: async () => [],
      renameEntry: async () => { throw new Error("unused"); },
      upload: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("offline");
        return { kind: "file", modifiedAt: new Date().toISOString(), name: file.name,
          path: `Inputs/${file.name}`, revision: "a".repeat(64), size: file.size };
      },
    };
    const platform: PlatformBridge = {
      kind: "browser",
      notify: async () => undefined,
      openDownloadedFile: async () => undefined,
      saveDownload: async () => undefined,
      selectDirectory: async () => null,
      selectFiles: async () => [{ file, name: file.name, size: file.size, type: file.type }],
    };
    const user = userEvent.setup();

    render(<FileActions api={api} directory="Inputs" platform={platform} projectId="p1" />);
    await user.click(screen.getByRole("button", { name: "导入文件" }));
    expect(await screen.findByText("失败")).toBeVisible();
    expect(screen.queryByText("已完成")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重试 meter.csv" }));
    expect(await screen.findByText("已完成")).toBeVisible();
    expect(attempts).toBe(2);
  });
});
