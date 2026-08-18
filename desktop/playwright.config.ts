import { defineConfig } from "@playwright/test";

export default defineConfig({
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  testDir: "./e2e",
  use: { trace: "retain-on-failure" },
});
