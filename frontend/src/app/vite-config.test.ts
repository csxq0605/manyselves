import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const viteConfig = readFileSync("../frontend/vite.config.ts", "utf8");

describe("vite config", () => {
  it("keeps old hashed chunks so open tabs can lazy-load routes after rebuilds", () => {
    expect(viteConfig).toContain("emptyOutDir: false");
  });
});
