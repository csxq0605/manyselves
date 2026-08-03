import { fileURLToPath } from "node:url";
import type { BrowserWindowConstructorOptions, WebContents } from "electron";
import { shell } from "electron";

export function createWindowOptions(preloadPath = fileURLToPath(new URL("./preload.cjs", import.meta.url))): BrowserWindowConstructorOptions {
  return {
    height: 900,
    minHeight: 640,
    minWidth: 960,
    show: false,
    width: 1440,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: preloadPath,
      sandbox: true,
      webSecurity: true,
    },
  };
}

export function isTrustedRendererUrl(url: string, trustedUrl?: string): boolean {
  if (!trustedUrl) return false;
  const parsed = new URL(url);
  const trusted = new URL(trustedUrl);
  if (trusted.protocol === "http:" || trusted.protocol === "https:") {
    return parsed.origin === trusted.origin;
  }
  return trusted.protocol === "file:"
    && parsed.protocol === "file:"
    && parsed.host === trusted.host
    && parsed.pathname === trusted.pathname;
}

export function hardenNavigation(contents: WebContents, trustedUrl: () => string): void {
  contents.on("will-navigate", (event, targetUrl) => {
    if (!isTrustedRendererUrl(targetUrl, trustedUrl())) event.preventDefault();
  });
  contents.setWindowOpenHandler(({ url }) => {
    if (new URL(url).protocol === "https:") void shell.openExternal(url);
    return { action: "deny" };
  });
}
