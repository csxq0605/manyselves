import { rename, unlink, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import type { BrowserWindow } from "electron";
import { dialog, shell } from "electron";

export class DownloadRegistry {
  private readonly paths = new Set<string>();
  add(path: string): void { this.paths.add(resolve(path)); }
  has(path: string): boolean { return this.paths.has(resolve(path)); }
}

export async function saveDownload(
  window: BrowserWindow,
  bytes: Uint8Array,
  suggestedName: string,
  registry: DownloadRegistry,
): Promise<string | null> {
  const result = await dialog.showSaveDialog(window, { defaultPath: basename(suggestedName) });
  if (result.canceled || !result.filePath) return null;
  const target = resolve(result.filePath);
  const partial = resolve(dirname(target), `.${basename(target)}.partial`);
  try {
    await writeFile(partial, bytes, { flag: "wx" });
    await rename(partial, target);
    registry.add(target);
    return target;
  } catch (error) {
    await unlink(partial).catch(() => undefined);
    throw error;
  }
}

export async function openDownloadedFile(path: string, registry: DownloadRegistry): Promise<void> {
  if (!registry.has(path)) throw Object.assign(new Error("Path was not issued by a completed download"), { code: "UNTRUSTED_DOWNLOAD_PATH" });
  const error = await shell.openPath(resolve(path));
  if (error) throw new Error(error);
}
