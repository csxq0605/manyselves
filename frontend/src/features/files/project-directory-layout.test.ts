import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync("src/features/files/project-directory.css", "utf8");

function cssBlock(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{(?<body>[^}]*)\\}`, "s").exec(css);
  return match?.groups?.body ?? "";
}

describe("project directory layout CSS", () => {
  it("uses a wide management surface and comfortable table padding", () => {
    const page = cssBlock(".project-directory");
    const row = cssBlock(".file-list__row");
    const viewport = cssBlock(".file-list__viewport");
    const outputViewport = cssBlock(".output-tabs__viewport");

    expect(page).toContain("margin: 0 auto");
    expect(page).toContain("max-width: min(1600px, calc(100vw - 96px))");
    expect(row).toContain("min-height: 46px");
    expect(row).toContain("padding: 9px 18px");
    expect(viewport).toContain("min-height: 220px");
    expect(outputViewport).not.toContain("min-height");
  });
});
