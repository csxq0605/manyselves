import type { BrowserWindow, MenuItemConstructorOptions } from "electron";
import { Menu } from "electron";

import { channels } from "../ipc-contract.js";

export type DesktopShortcut = "close-tab" | "interrupt" | "project-switcher" | "quick-file-search" | "save" | "send-message";

export function installApplicationMenu(window: BrowserWindow): void {
  const send = (action: DesktopShortcut) => window.webContents.send(channels.shortcut, action);
  const template: MenuItemConstructorOptions[] = [{
    label: "Manyselves",
    submenu: [
      { accelerator: "CmdOrCtrl+K", click: () => send("project-switcher"), label: "切换项目" },
      { accelerator: "CmdOrCtrl+P", click: () => send("quick-file-search"), label: "搜索文件" },
      { accelerator: "CmdOrCtrl+S", click: () => send("save"), label: "保存" },
      { accelerator: "CmdOrCtrl+W", click: () => send("close-tab"), label: "关闭标签" },
      { accelerator: "CmdOrCtrl+Enter", click: () => send("send-message"), label: "发送消息" },
      { accelerator: "Escape", click: () => send("interrupt"), label: "中断" },
    ],
  }];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}
