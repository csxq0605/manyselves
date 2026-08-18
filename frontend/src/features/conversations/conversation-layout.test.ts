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
    expect(main).toContain("flex-direction: column");
    expect(main).toContain("overflow-y: auto");
    expect(main).toContain("overscroll-behavior: contain");
  });

  it("bottom-aligns short history without clipping long scrollable history", () => {
    const messageList = cssBlock(".message-list");

    expect(messageList).toContain("margin: auto auto 0");
    expect(messageList).not.toContain("align-self: flex-end");
  });

  it("keeps the floating agent runtime panel compact and internally scrollable", () => {
    const runtimeList = cssBlock(".agent-runtime__list");

    expect(runtimeList).toContain("max-height: min(260px, 42dvh)");
    expect(runtimeList).toContain("overflow-y: auto");
    expect(runtimeList).toContain("overscroll-behavior: contain");
    expect(runtimeList).toContain("scrollbar-gutter: stable");
  });
});
