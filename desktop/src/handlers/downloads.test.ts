import { describe, expect, it, vi } from "vitest";

vi.mock("electron", () => ({ dialog: {}, shell: { openPath: vi.fn() } }));

import { DownloadRegistry, openDownloadedFile } from "./downloads.js";

describe("download registry", () => {
  it("rejects paths that were not issued by a completed download", async () => {
    await expect(openDownloadedFile("C:/Windows/System32/cmd.exe", new DownloadRegistry()))
      .rejects.toMatchObject({ code: "UNTRUSTED_DOWNLOAD_PATH" });
  });
});
