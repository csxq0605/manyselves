import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync("src/features/runtime/runtime-page.css", "utf8");

function cssBlock(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{(?<body>[^}]*)\\}`, "s").exec(css);
  return match?.groups?.body ?? "";
}

describe("runtime layout CSS", () => {
  it("keeps each runtime card bounded and scrolls card contents internally", () => {
    const page = cssBlock(".runtime-page, .logs-page");
    const hero = cssBlock(".runtime-page__hero");
    const card = cssBlock(".operations-card");
    const list = cssBlock(".operations-card > .operations-list");
    const errors = cssBlock(".runtime-page__errors");

    expect(page).toContain("margin: 0 auto");
    expect(page).toContain("max-width: min(1600px, calc(100vw - 96px))");
    expect(hero).toContain("background: #fff");
    expect(hero).not.toContain("radial-gradient");
    expect(card).toContain("display: flex");
    expect(card).toContain("flex-direction: column");
    expect(card).toContain("max-height: min(320px, 34dvh)");
    expect(card).toContain("overflow: hidden");
    expect(list).toContain("overflow-y: auto");
    expect(list).toContain("overscroll-behavior: contain");
    expect(list).toContain("scrollbar-gutter: stable");
    expect(errors).toContain("max-height: min(220px, 28dvh)");
  });
});
