import { defineConfig } from "vitest/config";

export default defineConfig({ test: { exclude: ["dist/**", "e2e/**", "release/**", "node_modules/**"] } });
