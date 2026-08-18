import { lstat, readFile, readdir } from "node:fs/promises";
import { basename, relative, resolve, sep } from "node:path";
import type { BrowserWindow } from "electron";
import { dialog } from "electron";

import type { NativeDirectoryRef, NativeFileRef } from "../ipc-contract.js";

function mimeType(path: string): string {
  const extension = path.toLowerCase().split(".").pop();
  return extension === "json" ? "application/json"
    : extension === "csv" ? "text/csv"
      : extension === "md" ? "text/markdown"
        : extension === "png" ? "image/png"
          : extension === "jpg" || extension === "jpeg" ? "image/jpeg"
            : "application/octet-stream";
}

async function nativeFile(path: string, relativePath?: string): Promise<NativeFileRef> {
  const bytes = await readFile(path);
  return {
    bytes: new Uint8Array(bytes),
    name: basename(path),
    ...(relativePath ? { relativePath } : {}),
    size: bytes.byteLength,
    type: mimeType(path),
  };
}

export async function enumerateImportDirectory(root: string): Promise<NativeDirectoryRef> {
  const absoluteRoot = resolve(root);
  const files: NativeFileRef[] = [];
  const skipped: string[] = [];
  async function walk(directory: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      const display = relative(absoluteRoot, path).split(sep).join("/");
      const metadata = await lstat(path);
      if (metadata.isSymbolicLink()) { skipped.push(display); continue; }
      if (metadata.isDirectory()) await walk(path);
      else if (metadata.isFile()) files.push(await nativeFile(path, display));
      else skipped.push(display);
    }
  }
  await walk(absoluteRoot);
  files.sort((left, right) => (left.relativePath ?? left.name).localeCompare(right.relativePath ?? right.name));
  return { files, name: basename(absoluteRoot), skipped };
}

export async function selectFiles(window: BrowserWindow): Promise<readonly NativeFileRef[]> {
  const result = await dialog.showOpenDialog(window, { properties: ["openFile", "multiSelections"] });
  if (result.canceled) return [];
  return Promise.all(result.filePaths.map((path) => nativeFile(path)));
}

export async function selectDirectory(window: BrowserWindow): Promise<NativeDirectoryRef | null> {
  const result = await dialog.showOpenDialog(window, { properties: ["openDirectory"] });
  const root = result.filePaths[0];
  return result.canceled || !root ? null : enumerateImportDirectory(root);
}
