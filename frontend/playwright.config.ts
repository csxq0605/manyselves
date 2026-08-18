import { defineConfig } from "@playwright/test";

export default defineConfig({
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  testDir: "./e2e",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4173",
    reuseExistingServer: true,
    timeout: 120_000,
    url: "http://127.0.0.1:4173",
  },
});
