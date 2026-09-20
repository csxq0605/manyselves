import { describe, expect, it, vi } from "vitest";

import { ElectronPlatformBridge, type RendererDesktopApi } from "./electron-platform";

function api(): RendererDesktopApi {
  return {
    notify: vi.fn(), openDownloadedFile: vi.fn(), saveDownload: vi.fn(), selectDirectory: vi.fn().mockResolvedValue(null),
    selectFiles: vi.fn().mockResolvedValue([{ bytes: new Uint8Array([65]), name: "a.txt", size: 1, type: "text/plain" }]),
  };
}

describe("ElectronPlatformBridge", () => {
  it("still saves the verified bytes when a browser attachment URL is provided", async () => {
    const desktop = api();
    const bridge = new ElectronPlatformBridge(desktop);
    await bridge.saveDownload({
      blob: new Blob(["report"]),
      suggestedName: "report.docx",
      sourceUrl: "/api/v1/projects/test/files/download?path=Outputs%2Freport.docx",
    });
    expect(desktop.saveDownload).toHaveBeenCalledOnce();
    const saved = vi.mocked(desktop.saveDownload).mock.calls[0]?.[0];
    expect(saved?.suggestedName).toBe("report.docx");
    expect(Array.from(saved?.bytes ?? [])).toEqual([114, 101, 112, 111, 114, 116]);
    expect(Object.keys(saved ?? {}).sort()).toEqual(["bytes", "suggestedName"]);
  });

  it("implements the locked platform bridge without exposing generic IPC", async () => {
    const bridge = new ElectronPlatformBridge(api());
    const files = await bridge.selectFiles();
    expect(bridge.kind).toBe("electron");
    expect(files[0]?.name).toBe("a.txt");
    expect(await files[0]?.file.text()).toBe("A");
    expect("invoke" in bridge).toBe(false);
  });
});
