import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { app, BrowserWindow, ipcMain } from "electron";

import { channels, downloadedPathSchema, notificationSchema, saveDownloadSchema } from "./ipc-contract.js";
import { DownloadRegistry, openDownloadedFile, saveDownload } from "./handlers/downloads.js";
import { selectDirectory, selectFiles } from "./handlers/files.js";
import { showNotification } from "./handlers/notifications.js";
import { installApplicationMenu } from "./handlers/shortcuts.js";
import { createWindowOptions, hardenNavigation, isTrustedRendererUrl } from "./security.js";

let mainWindow: BrowserWindow | null = null;

function rendererUrl(): string {
  const developmentUrl = process.env.MANYSELVES_DESKTOP_DEV_URL;
  if (developmentUrl) return developmentUrl;
  return pathToFileURL(join(process.resourcesPath, "frontend", "index.html")).toString();
}

function requireTrustedSender(url: string): void {
  if (!isTrustedRendererUrl(url, rendererUrl())) {
    throw new Error("UNTRUSTED_IPC_SENDER");
  }
}

function registerPlatformHandlers(getWindow: () => BrowserWindow): void {
  const downloads = new DownloadRegistry();
  const trusted = (url: string) => requireTrustedSender(url);
  ipcMain.handle(channels.selectFiles, async (event) => { trusted(event.senderFrame?.url ?? ""); return selectFiles(getWindow()); });
  ipcMain.handle(channels.selectDirectory, async (event) => { trusted(event.senderFrame?.url ?? ""); return selectDirectory(getWindow()); });
  ipcMain.handle(channels.saveDownload, async (event, value: unknown) => {
    trusted(event.senderFrame?.url ?? "");
    const input = saveDownloadSchema.parse(value);
    return saveDownload(getWindow(), input.bytes, input.suggestedName, downloads);
  });
  ipcMain.handle(channels.openDownloadedFile, async (event, value: unknown) => {
    trusted(event.senderFrame?.url ?? "");
    await openDownloadedFile(downloadedPathSchema.parse(value), downloads);
  });
  ipcMain.handle(channels.notify, (event, value: unknown) => {
    trusted(event.senderFrame?.url ?? "");
    showNotification(notificationSchema.parse(value));
  });
}

async function createMainWindow(): Promise<void> {
  const target = rendererUrl();
  const window = new BrowserWindow(createWindowOptions());
  mainWindow = window;
  installApplicationMenu(window);
  hardenNavigation(window.webContents, () => target);
  window.once("ready-to-show", () => window.show());
  window.on("closed", () => { mainWindow = null; });
  await window.loadURL(target);
}

app.whenReady().then(async () => {
  registerPlatformHandlers(() => {
    if (mainWindow === null || mainWindow.isDestroyed()) throw new Error("DESKTOP_WINDOW_UNAVAILABLE");
    return mainWindow;
  });
  await createMainWindow();
  app.on("activate", () => { if (mainWindow === null) void createMainWindow(); });
});
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
