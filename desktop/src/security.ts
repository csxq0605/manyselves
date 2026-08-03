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

export function isTrustedRendererUrl(url: string, developmentUrl?: string): boolean {
  const parsed = new URL(url);
  if (developmentUrl && parsed.origin === new URL(developmentUrl).origin) return true;
  return parsed.protocol === "file:" && parsed.pathname.endsWith("/index.html");
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
