const { contextBridge, ipcRenderer } = require("electron");

const channels = Object.freeze({
  notify: "manyselves:notify",
  openDownloadedFile: "manyselves:open-downloaded-file",
  saveDownload: "manyselves:save-download",
  selectDirectory: "manyselves:select-directory",
  selectFiles: "manyselves:select-files",
  shortcut: "manyselves:shortcut",
});
const api = Object.freeze({
  notify: (input) => ipcRenderer.invoke(channels.notify, input),
  openDownloadedFile: (path) => ipcRenderer.invoke(channels.openDownloadedFile, path),
  saveDownload: (input) => ipcRenderer.invoke(channels.saveDownload, input),
  selectDirectory: () => ipcRenderer.invoke(channels.selectDirectory),
  selectFiles: () => ipcRenderer.invoke(channels.selectFiles),
});
contextBridge.exposeInMainWorld("manyselvesDesktop", api);
ipcRenderer.on(channels.shortcut, (_event, action) => {
  window.dispatchEvent(new CustomEvent("manyselves:desktop-shortcut", { detail: action }));
});
