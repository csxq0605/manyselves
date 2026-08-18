import { describe, expect, it, vi } from "vitest";
import { installDesktopShortcutActions } from "./desktop-shortcuts";

describe("desktop shortcuts", () => {
  it("dispatches through existing React-facing controls", () => {
    const save = document.createElement("button");
    save.textContent = "保存";
    const click = vi.spyOn(save, "click");
    document.body.append(save);
    const cleanup = installDesktopShortcutActions();
    window.dispatchEvent(new CustomEvent("manyselves:desktop-shortcut", { detail: "save" }));
    expect(click).toHaveBeenCalledOnce();
    cleanup();
    save.remove();
  });
});
