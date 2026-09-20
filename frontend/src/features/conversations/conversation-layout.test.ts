import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

const css = readFileSync("src/features/conversations/conversation.css", "utf8");

function cssBlock(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{(?<body>[^}]*)\\}`, "s").exec(css);
  return match?.groups?.body ?? "";
}

describe("conversation layout CSS", () => {
  it("locks the workspace height and scrolls only the message history pane", () => {
    const workspace = cssBlock(".conversation-workspace");
    const main = cssBlock(".conversation-workspace__main");

    expect(workspace).toContain("height: 100dvh");
    expect(workspace).toContain("max-height: 100dvh");
    expect(workspace).toContain("overflow: hidden");
    expect(workspace).toContain("grid-template-rows: auto minmax(0, 1fr) auto auto");
    expect(main).toContain("flex-direction: column");
    expect(main).toContain("overflow-y: auto");
    expect(main).toContain("overscroll-behavior: contain");
  });

  it("bottom-aligns short history without clipping long scrollable history", () => {
    const messageList = cssBlock(".message-list");

    expect(messageList).toContain("margin: auto auto 0");
    expect(messageList).not.toContain("align-self: flex-end");
  });

  it("keeps the inline agent runtime panel compact and internally scrollable", () => {
    const runtime = cssBlock(".agent-runtime");
    const runtimeList = cssBlock(".agent-runtime__list");

    expect(runtime).not.toContain("position: fixed");
    expect(runtime).toContain("position: static");
    expect(runtimeList).toContain("max-height: min(260px, 42dvh)");
    expect(runtimeList).toContain("overflow-y: auto");
    expect(runtimeList).toContain("overscroll-behavior: contain");
    expect(runtimeList).toContain("scrollbar-gutter: stable");
  });

  it("places conversation actions in their own row instead of over the composer", () => {
    const overflow = cssBlock(".conversation-overflow");

    expect(overflow).toContain("position: relative");
    expect(overflow).not.toContain("position: absolute");
  });

  it("uses the same phone breakpoint as the project shell", () => {
    expect(css).toContain("@media (max-width: 520px)");
    expect(css).not.toContain("@media (max-width: 700px)");
  });
});
