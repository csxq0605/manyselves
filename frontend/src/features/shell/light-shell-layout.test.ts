import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync("src/features/shell/light-shell.css", "utf8");

function cssBlock(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{(?<body>[^}]*)\\}`, "s").exec(css);
  return match?.groups?.body ?? "";
}

describe("light shell layout CSS", () => {
  it("removes the browser body margin so a viewport-height sidebar is not clipped", () => {
    expect(cssBlock("body")).toContain("margin: 0");
  });

  it("keeps the desktop project sidebar in the viewport while routed content scrolls", () => {
    const sidebar = cssBlock(".sidebar");

    expect(sidebar).toContain("position: sticky");
    expect(sidebar).toContain("top: 0");
    expect(sidebar).toContain("height: 100dvh");
    expect(sidebar).toContain("overflow: hidden");
  });

  it("keeps compact desktop widths in the two-column shell", () => {
    expect(css).toContain("@media (max-width: 520px)");
    expect(css).not.toContain("@media (max-width: 700px)");
  });
});
