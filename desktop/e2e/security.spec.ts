import { _electron as electron, expect, test } from "@playwright/test";
import { resolve } from "node:path";

test("denies untrusted navigation, windows, paths, and renderer capabilities", async () => {
  const app = await electron.launch({
    args: ["--disable-gpu", resolve(import.meta.dirname, "..")],
    env: { ...process.env, MANYSELVES_DESKTOP_DEV_URL: "http://127.0.0.1:4173" },
  });
  const page = await app.firstWindow();
  try {
    const originalUrl = page.url();
    expect(await page.evaluate(() => ({ process: typeof process, require: typeof require }))).toEqual({
      process: "undefined", require: "undefined",
    });
    expect(await page.evaluate(() => window.open("data:text/html,untrusted") === null)).toBe(true);
    expect(app.windows()).toHaveLength(1);

    const pathError = await page.evaluate(async () => {
      try {
        await window.manyselvesDesktop?.openDownloadedFile("C:/Windows/System32/cmd.exe");
        return "accepted";
      } catch (error) {
        return String(error);
      }
    });
    expect(pathError).toContain("Path was not issued by a completed download");

    await page.evaluate(() => { window.location.href = "data:text/html,untrusted"; });
    await page.waitForTimeout(100);
    expect(page.url()).toBe(originalUrl);

    const untrustedHasBridge = await app.evaluate(async ({ BrowserWindow }) => {
      const window = new BrowserWindow({ show: false, webPreferences: { sandbox: true } });
      await window.loadURL("data:text/html,untrusted");
      const exposed = await window.webContents.executeJavaScript("typeof window.manyselvesDesktop");
      window.destroy();
      return exposed;
    });
    expect(untrustedHasBridge).toBe("undefined");
  } finally {
    await app.close();
  }
});
