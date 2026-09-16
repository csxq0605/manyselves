import react from "@vitejs/plugin-react";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

import {
  duplicatePublicAssets,
  resolvePublicBaseUrl,
} from "./build/duplicate-public-assets.ts";

const outputDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "dist");

export default defineConfig(({ command }) => {
  const publicBaseUrl = resolvePublicBaseUrl(command);
  return {
    base: publicBaseUrl,
    build: {
      emptyOutDir: false,
    },
    plugins: [
      react(),
      {
        apply: "build",
        async closeBundle() {
          await duplicatePublicAssets(outputDirectory, publicBaseUrl);
        },
        name: "duplicate-prefixed-assets",
      },
    ],
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.{ts,tsx}"],
      setupFiles: ["./src/test/setup.ts"],
    },
  };
});
