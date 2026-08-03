export function installDesktopShortcutActions(target: Window = window): () => void {
  const handler = (event: Event) => {
    const action = (event as CustomEvent<string>).detail;
    const selectors: Record<string, string> = {
      "close-tab": '[aria-label^="关闭 "]',
      interrupt: 'button[type="button"]',
      "project-switcher": 'select[aria-label="项目"]',
      "quick-file-search": '[role="tree"]',
      save: 'button[type="button"]',
      "send-message": 'button[type="button"]',
    };
    const element = document.querySelector<HTMLElement>(selectors[action] ?? "");
    if (action === "save") {
      [...document.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === "保存")?.click();
    } else if (action === "send-message") {
      [...document.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === "发送")?.click();
    } else if (action === "interrupt") {
      [...document.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === "停止生成")?.click();
    } else if (action === "close-tab") element?.click();
    else element?.focus();
  };
  target.addEventListener("manyselves:desktop-shortcut", handler);
  return () => target.removeEventListener("manyselves:desktop-shortcut", handler);
}
