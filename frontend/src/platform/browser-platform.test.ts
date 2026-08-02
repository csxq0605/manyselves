import { describe, expect, it, vi } from "vitest";

import {
  BrowserPlatformBridge,
  createDomBrowserPlatformDriver,
} from "./browser-platform";

describe("BrowserPlatformBridge", () => {
  it("returns uploaded File objects without exposing local paths", async () => {
    const file = new File(["x"], "a.txt", { type: "text/plain" });
    const platform = new BrowserPlatformBridge({
      notify: async () => undefined,
      saveDownload: async () => undefined,
      selectDirectory: async () => null,
      selectFiles: async () => [file],
    });

    const selected = await platform.selectFiles();

    expect(selected).toEqual([
      {
        file,
        name: "a.txt",
        size: 1,
        type: "text/plain",
      },
    ]);
    expect(selected[0]).not.toHaveProperty("path");
  });

  it("normalizes a selected directory without adding client paths", async () => {
    const file = new File(["data"], "report.csv", { type: "text/csv" });
    const platform = new BrowserPlatformBridge({
      notify: async () => undefined,
      openDownloadedFile: async () => undefined,
      saveDownload: async () => undefined,
      selectDirectory: async () => ({ files: [file], name: "Inputs" }),
      selectFiles: async () => [],
    });

    const selected = await platform.selectDirectory();

    expect(selected).toEqual({
      files: [
        {
          file,
          name: "report.csv",
          size: 4,
          type: "text/csv",
        },
      ],
      name: "Inputs",
    });
    expect(selected?.files[0]).not.toHaveProperty("path");
  });

  it("delegates downloads and notifications through the browser driver", async () => {
    const effects: unknown[] = [];
    const platform = new BrowserPlatformBridge({
      notify: async (input) => {
        effects.push(["notify", input]);
      },
      openDownloadedFile: async (path) => {
        effects.push(["open", path]);
      },
      saveDownload: async (input) => {
        effects.push(["download", input]);
      },
      selectDirectory: async () => null,
      selectFiles: async () => [],
    });
    const download = {
      blob: new Blob(["report"]),
      suggestedName: "report.txt",
    };

    await platform.saveDownload(download);
    await platform.notify({ body: "ready", title: "Manyselves" });

    expect(effects).toEqual([
      ["download", download],
      ["notify", { body: "ready", title: "Manyselves" }],
    ]);
  });

  it("does not claim support for opening arbitrary local paths", async () => {
    const platform = new BrowserPlatformBridge({
      notify: async () => undefined,
      openDownloadedFile: async () => undefined,
      saveDownload: async () => undefined,
      selectDirectory: async () => null,
      selectFiles: async () => [],
    });

    await expect(platform.openDownloadedFile("C:\\private\\report.txt")).rejects.toThrow(
      "not supported in the browser",
    );
  });
});

describe("DOM browser platform driver", () => {
  it("removes the temporary picker after returning selected files", async () => {
    const file = new File(["x"], "a.txt", { type: "text/plain" });
    const driver = createDomBrowserPlatformDriver({ document });

    const selection = driver.selectFiles();
    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    input?.dispatchEvent(new Event("change"));

    await expect(selection).resolves.toEqual([file]);
    expect(document.querySelector('input[type="file"]')).toBeNull();
  });

  it("derives the selected directory name from webkit relative paths", async () => {
    const file = new File(["x"], "a.txt");
    Object.defineProperty(file, "webkitRelativePath", {
      configurable: true,
      value: "Inputs/nested/a.txt",
    });
    const driver = createDomBrowserPlatformDriver({ document });

    const selection = driver.selectDirectory();
    const input = document.querySelector<HTMLInputElement>(
      'input[type="file"][webkitdirectory]',
    );
    expect(input).not.toBeNull();
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    input?.dispatchEvent(new Event("change"));

    await expect(selection).resolves.toEqual({ files: [file], name: "Inputs" });
  });

  it("downloads a blob with a temporary object URL and cleans it up", async () => {
    const effects: unknown[] = [];
    const driver = createDomBrowserPlatformDriver({
      createObjectUrl: () => "blob:report",
      document,
      revokeObjectUrl: (url) => {
        effects.push(["revoke", url]);
      },
    });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function clickAnchor(this: HTMLAnchorElement) {
        effects.push(["click", this.download, this.href]);
      });

    await driver.saveDownload({
      blob: new Blob(["report"]),
      suggestedName: "report.txt",
    });

    expect(effects).toEqual([
      ["click", "report.txt", "blob:report"],
      ["revoke", "blob:report"],
    ]);
    expect(document.querySelector('a[download="report.txt"]')).toBeNull();
    click.mockRestore();
  });
});
