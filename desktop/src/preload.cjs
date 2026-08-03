const { contextBridge, ipcRenderer } = require("electron");

const channels = Object.freeze({
  notify: "manyselves:notify",
  openDownloadedFile: "manyselves:open-downloaded-file",
  saveDownload: "manyselves:save-download",
  selectDirectory: "manyselves:select-directory",
  selectFiles: "manyselves:select-files",
  shortcut: "manyselves:shortcut",
  tokenDelete: "manyselves:token-delete",
  tokenGet: "manyselves:token-get",
  tokenSet: "manyselves:token-set",
});
const api = Object.freeze({
  notify: (input) => ipcRenderer.invoke(channels.notify, input),
  openDownloadedFile: (path) => ipcRenderer.invoke(channels.openDownloadedFile, path),
  saveDownload: (input) => ipcRenderer.invoke(channels.saveDownload, input),
  selectDirectory: () => ipcRenderer.invoke(channels.selectDirectory),
  selectFiles: () => ipcRenderer.invoke(channels.selectFiles),
  secureToken: Object.freeze({
    delete: () => ipcRenderer.invoke(channels.tokenDelete),
    get: () => ipcRenderer.invoke(channels.tokenGet),
    set: (token) => ipcRenderer.invoke(channels.tokenSet, token),
  }),
});
contextBridge.exposeInMainWorld("manyselvesDesktop", api);
ipcRenderer.on(channels.shortcut, (_event, action) => {
  window.dispatchEvent(new CustomEvent("manyselves:desktop-shortcut", { detail: action }));
});
