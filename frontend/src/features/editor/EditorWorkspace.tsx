import { type ComponentType, lazy, Suspense, useEffect, useRef, useState } from "react";
import { useStore } from "zustand";

import { ApiError } from "../../api/gateway";
import type { DraftRepository } from "./draft-storage";
import {
  createIndexedDbDraftRepository,
  loadOpenTabPaths,
  saveOpenTabPaths,
} from "./draft-storage";
import type { EditorFileApi, FileContent } from "./editor-api";
import type { EditorStore, EditorTab } from "./editor-store";
import { EditorTabs } from "./EditorTabs";
import { SaveConflictDialog } from "./SaveConflictDialog";
import type { TextEditorProps } from "./TextEditor";

const DefaultTextEditor = lazy(async () => {
  const module = await import("./TextEditor");
  return { default: module.TextEditor };
});

export interface EditorWorkspaceProps {
  readonly api: EditorFileApi;
  readonly editorComponent?: ComponentType<TextEditorProps>;
  readonly projectId: string;
  readonly repository?: DraftRepository;
  readonly serverUrl: string;
  readonly store: EditorStore;
}

export function EditorWorkspace({
  api,
  editorComponent: EditorComponent = DefaultTextEditor,
  projectId,
  repository,
  serverUrl,
  store,
}: EditorWorkspaceProps) {
  const [draftRepository] = useState(() => repository ?? createIndexedDbDraftRepository());
  const activePath = useStore(store, (state) => state.activePath);
  const activeTab = useStore(
    store,
    (state) => state.tabs.find((tab) => tab.path === state.activePath) ?? null,
  );
  const dirty = useStore(store, (state) => state.tabs.some((tab) => tab.dirty));
  const [conflictLatest, setConflictLatest] = useState<FileContent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const persistenceByPath = useRef(new Map<string, Promise<void>>());

  useEffect(() => {
    let cancelled = false;
    const restoredTabPaths = loadOpenTabPaths(window.localStorage, serverUrl, projectId);
    void draftRepository.list(serverUrl, projectId).then(async (drafts) => {
      const draftsByPath = new Map(drafts.map((draft) => [draft.path, draft]));
      const paths = new Set([
        ...restoredTabPaths,
        ...draftsByPath.keys(),
      ]);
      for (const path of paths) {
        if (cancelled) return;
        try {
          const latest = await api.read(projectId, path);
          if (cancelled) return;
          const draft = draftsByPath.get(path);
          if (draft) {
            store.getState().restoreDraft(
              projectId,
              latest,
              draft.content,
              draft.serverRevision,
            );
          } else {
            store.getState().openFile(projectId, latest);
          }
        } catch {
          // An inaccessible draft remains in IndexedDB for explicit later recovery.
        }
      }
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [api, draftRepository, projectId, serverUrl, store]);

  useEffect(() => store.subscribe((state) => {
    saveOpenTabPaths(
      window.localStorage,
      serverUrl,
      projectId,
      state.tabs.filter((tab) => tab.projectId === projectId).map((tab) => tab.path),
    );
  }), [projectId, serverUrl, store]);

  useEffect(() => {
    function warnOnUnload(event: BeforeUnloadEvent) {
      if (!store.getState().tabs.some((tab) => tab.dirty)) return;
      event.preventDefault();
      event.returnValue = "";
    }
    window.addEventListener("beforeunload", warnOnUnload);
    return () => window.removeEventListener("beforeunload", warnOnUnload);
  }, [store]);

  function persistDraft(tab: EditorTab, content: string): Promise<void> {
    const previous = persistenceByPath.current.get(tab.path) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(() => draftRepository.save({
      content,
      path: tab.path,
      projectId,
      serverRevision: tab.serverRevision,
      serverUrl,
      updatedAt: Date.now(),
    }));
    persistenceByPath.current.set(tab.path, next);
    return next;
  }

  async function saveWithRevision(tab: EditorTab, baseRevision: string) {
    setSaving(true);
    setError(null);
    try {
      await (persistenceByPath.current.get(tab.path) ?? Promise.resolve()).catch(() => {
        setError("本地草稿存储失败；正在尝试直接保存到服务器");
      });
      const saved = await api.save(projectId, tab.path, tab.draftContent, baseRevision);
      store.getState().applySaved(saved);
      const current = store.getState().tabs.find((candidate) => candidate.path === tab.path);
      if (current?.dirty) {
        await persistDraft(current, current.draftContent).catch(() => {
          setError("服务器已保存较早版本，但较新的本地输入未能写入草稿存储");
        });
      } else {
        await draftRepository.remove(serverUrl, projectId, tab.path).catch(() => {
          setError("服务器保存成功，但本地旧草稿清理失败");
        });
      }
      setConflictLatest(null);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        try {
          const latest = await api.read(projectId, tab.path);
          store.getState().markServerChanged(tab.path, latest.revision);
          setConflictLatest(latest);
        } catch {
          setError("无法读取最新服务器版本，草稿仍保留在本地");
        }
      } else {
        setError(reason instanceof ApiError ? reason.message : "保存失败，草稿仍保留在本地");
      }
    } finally {
      setSaving(false);
    }
  }

  async function saveActive() {
    if (!activeTab || !activeTab.dirty) return;
    if (activeTab.externalRevision) {
      try {
        setConflictLatest(await api.read(projectId, activeTab.path));
      } catch {
        setError("无法读取最新服务器版本，草稿仍保留在本地");
      }
      return;
    }
    await saveWithRevision(activeTab, activeTab.serverRevision);
  }

  return (
    <section aria-label="文件编辑器" className="editor-workspace">
      <EditorTabs store={store} />
      {activeTab ? (
        <>
          <div className="editor-toolbar">
            <span>{activePath}</span>
            {activeTab.externalRevision ? <span role="status">服务器文件已更新</span> : null}
            <button disabled={!activeTab.dirty || saving} onClick={() => void saveActive()} type="button">
              {saving ? "保存中…" : "保存"}
            </button>
          </div>
          <Suspense fallback={<p role="status">正在加载 Monaco 编辑器…</p>}>
            <EditorComponent
              key={activeTab.path}
              onChange={(content) => {
                store.getState().updateDraft(activeTab.path, content);
                void persistDraft(activeTab, content).catch(() => {
                  setError("本地草稿存储失败；请在离开页面前手动保存到服务器");
                });
              }}
              onSave={() => void saveActive()}
              onViewStateChange={(cursor, viewState) => {
                store.getState().setViewState(activeTab.path, cursor, viewState);
              }}
              tab={activeTab}
            />
          </Suspense>
        </>
      ) : <p className="pane-muted">从服务器文件树中打开可编辑文本文件。</p>}
      {error ? <p role="alert">{error}</p> : null}
      {conflictLatest && activeTab ? (
        <SaveConflictDialog
          onKeepDraft={() => setConflictLatest(null)}
          onOverwrite={() => {
            if (!window.confirm("确认使用当前本地草稿覆盖最新服务器版本？")) return;
            void saveWithRevision(activeTab, conflictLatest.revision);
          }}
          onReload={() => {
            store.getState().reloadFromServer(conflictLatest);
            void draftRepository.remove(serverUrl, projectId, conflictLatest.path);
            setConflictLatest(null);
          }}
          path={activeTab.path}
        />
      ) : null}
      {dirty ? <span className="draft-badge">本地草稿</span> : null}
    </section>
  );
}
