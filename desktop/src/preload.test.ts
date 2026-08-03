import { describe, expect, it, vi } from "vitest";

vi.mock("electron", () => ({
  contextBridge: { exposeInMainWorld: vi.fn() },
  ipcRenderer: { invoke: vi.fn() },
}));

import { buildPreloadApi } from "./preload.js";

describe("preload bridge", () => {
  it("exposes only approved bridge methods", () => {
    expect(Object.keys(buildPreloadApi(vi.fn())).sort()).toEqual([
      "notify", "openDownloadedFile", "saveDownload", "secureToken", "selectDirectory", "selectFiles",
    ]);
  });
});
