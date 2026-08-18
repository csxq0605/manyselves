import { contextBridge, ipcRenderer } from "electron";

import { channels, type DesktopPreloadApi } from "./ipc-contract.js";

export function buildPreloadApi(invoke = ipcRenderer.invoke.bind(ipcRenderer)): DesktopPreloadApi {
  return Object.freeze({
    notify: (input: Parameters<DesktopPreloadApi["notify"]>[0]) => invoke(channels.notify, input),
    openDownloadedFile: (path: string) => invoke(channels.openDownloadedFile, path),
    saveDownload: (input: Parameters<DesktopPreloadApi["saveDownload"]>[0]) => invoke(channels.saveDownload, input),
    selectDirectory: () => invoke(channels.selectDirectory),
    selectFiles: () => invoke(channels.selectFiles),
  });
}

if (process.contextIsolated) {
  contextBridge.exposeInMainWorld("manyselvesDesktop", buildPreloadApi());
  ipcRenderer.on(channels.shortcut, (_event, action: string) => {
    window.dispatchEvent(new CustomEvent("manyselves:desktop-shortcut", { detail: action }));
  });
}
