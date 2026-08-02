import { useStore } from "zustand";

import type { EditorStore, EditorTab } from "./editor-store";

export interface EditorTabsProps {
  readonly confirmClose?: (tab: EditorTab) => boolean;
  readonly store: EditorStore;
}

function basename(path: string): string {
  return path.split("/").at(-1) ?? path;
}

export function EditorTabs({
  confirmClose = (tab) => window.confirm(`“${tab.path}”有未保存内容，仍要关闭吗？`),
  store,
}: EditorTabsProps) {
  const activePath = useStore(store, (state) => state.activePath);
  const tabs = useStore(store, (state) => state.tabs);

  return (
    <div aria-label="打开的文件" className="editor-tabs" role="tablist">
      {tabs.map((tab) => (
        <div className="editor-tab" key={tab.path}>
          <button
            aria-selected={activePath === tab.path}
            onClick={() => store.getState().activateTab(tab.path)}
            role="tab"
            type="button"
          >
            {basename(tab.path)}{tab.dirty ? " •" : ""}
          </button>
          <button
            aria-label={`关闭 ${tab.path}`}
            onClick={() => {
              if (!tab.dirty || confirmClose(tab)) store.getState().closeTab(tab.path);
            }}
            type="button"
          >×</button>
        </div>
      ))}
    </div>
  );
}
