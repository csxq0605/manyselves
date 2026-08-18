import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync("src/features/history/history-page.css", "utf8");

function cssBlock(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{(?<body>[^}]*)\\}`, "s").exec(css);
  return match?.groups?.body ?? "";
}

describe("history layout CSS", () => {
  it("uses a wider management layout without cramped list content", () => {
    const page = cssBlock(".history-page");
    const viewport = cssBlock(".history-page__viewport");
    const itemButton = cssBlock(".history-page__item-button");

    expect(page).toContain("margin: 0 auto");
    expect(page).toContain("max-width: min(1600px, calc(100vw - 96px))");
    expect(viewport).toContain("padding: 16px 18px");
    expect(itemButton).toContain("padding: 15px 18px");
  });
});
