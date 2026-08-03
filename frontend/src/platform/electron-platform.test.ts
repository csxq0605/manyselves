import { describe, expect, it, vi } from "vitest";

import { ElectronPlatformBridge, type RendererDesktopApi } from "./electron-platform";

function api(): RendererDesktopApi {
  return {
    notify: vi.fn(), openDownloadedFile: vi.fn(), saveDownload: vi.fn(), selectDirectory: vi.fn().mockResolvedValue(null),
    selectFiles: vi.fn().mockResolvedValue([{ bytes: new Uint8Array([65]), name: "a.txt", size: 1, type: "text/plain" }]),
  };
}

describe("ElectronPlatformBridge", () => {
  it("implements the locked platform bridge without exposing generic IPC", async () => {
    const bridge = new ElectronPlatformBridge(api());
    const files = await bridge.selectFiles();
    expect(bridge.kind).toBe("electron");
    expect(files[0]?.name).toBe("a.txt");
    expect(await files[0]?.file.text()).toBe("A");
    expect("invoke" in bridge).toBe(false);
  });
});
