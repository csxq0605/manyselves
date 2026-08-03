import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { resolve } from "node:path";

async function launch(): Promise<{ app: ElectronApplication; page: Page }> {
  const app = await electron.launch({
    args: ["--disable-gpu", resolve(import.meta.dirname, "..")],
    env: { ...process.env, MANYSELVES_DESKTOP_DEV_URL: "http://127.0.0.1:4173" },
  });
  const page = await app.firstWindow();
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/bootstrap") return route.fulfill({
      body: JSON.stringify({
        agents: { main: "Main Agent" }, conversations: [], maintenance: {}, project: { id: "project-1" },
        runtime: { active_session_id: "session-1", agent_statuses: { main: "idle" }, checkpoints: [], controller_client_id: null, debug: [], queues: [], ready: true, tasks: [], tools: [], workspace: null },
        settings: { control_lease_seconds: 30, sse_client_queue_capacity: 128, sse_replay_capacity: 512 }, streamId: "stream-1",
      }), contentType: "application/json",
    });
    if (path === "/api/v1/events") return route.fulfill({ body: "", contentType: "text/event-stream" });
    if (path === "/api/v1/projects") return route.fulfill({ body: '{"projects":[{"active":true,"id":"project-1"}]}', contentType: "application/json" });
    if (path.endsWith("/files/tree")) return route.fulfill({ body: '{"entries":[]}', contentType: "application/json" });
    if (path === "/api/v1/conversations") return route.fulfill({ body: '{"activeSessionId":null,"conversations":[]}', contentType: "application/json" });
    return route.fulfill({ status: 404 });
  });
  await page.reload();
  await page.waitForLoadState("domcontentloaded");
  return { app, page };
}

test("loads React through the allowlisted preload without renderer Node access", async () => {
  const { app, page } = await launch();
  try {
    await expect(page.getByText("Manyselves")).toBeVisible();
    expect(await page.evaluate(() => typeof process)).toBe("undefined");
    expect(await page.evaluate(() => Object.keys(window.manyselvesDesktop ?? {}).sort())).toEqual([
      "notify", "openDownloadedFile", "saveDownload", "secureToken", "selectDirectory", "selectFiles",
    ]);
    const preferences = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.webContents.getLastWebPreferences());
    expect(preferences).toMatchObject({ contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true });
  } finally { await app.close(); }
});
