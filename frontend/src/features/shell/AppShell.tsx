import { lazy, Suspense, useMemo, useState } from "react";
import { useStore } from "zustand";

import { useWorkspaceStore } from "../../app/store-context";
import type { ApiGateway, BootstrapSnapshot } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import { createEditorFileApi } from "../editor/editor-api";
import { createEditorStore } from "../editor/editor-store";
import { isEditableTextPath } from "../editor/editable-files";
import { createOperationApi } from "../preview/operation-api";
import { createPreviewApi } from "../preview/preview-api";
import { PythonRunAction } from "../preview/PythonRunAction";
import { ProjectWorkspace } from "../projects/ProjectWorkspace";
import { ConnectionBanner } from "./ConnectionBanner";
import "./app-shell.css";

const EditorWorkspace = lazy(async () => {
  const module = await import("../editor/EditorWorkspace");
  return { default: module.EditorWorkspace };
});

const PreviewWorkspace = lazy(async () => {
  const module = await import("../preview/PreviewWorkspace");
  return { default: module.PreviewWorkspace };
});

export interface AppShellProps {
  readonly bootstrap?: BootstrapSnapshot | undefined;
  readonly gateway?: ApiGateway;
  readonly platform?: PlatformBridge;
}

export function AppShell({ bootstrap, gateway, platform }: AppShellProps) {
  const drafts = useWorkspaceStore((store) => store.drafts);
  const setDraft = useWorkspaceStore((store) => store.setDraft);
  const activeDraftPath = useMemo(() => Object.keys(drafts)[0] ?? "scratchpad.md", [drafts]);
  const activeDraft = drafts[activeDraftPath] ?? "";
  const [openError, setOpenError] = useState<string | null>(null);
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const editorApi = useMemo(() => gateway ? createEditorFileApi(gateway) : null, [gateway]);
  const operationApi = useMemo(() => gateway ? createOperationApi(gateway) : null, [gateway]);
  const previewApi = useMemo(() => gateway ? createPreviewApi(gateway) : null, [gateway]);
  const [editorStore] = useState(() => createEditorStore());
  const activeEditorPath = useStore(editorStore, (store) => store.activePath);
  const hasDirtyEditorDrafts = useStore(
    editorStore,
    (store) => store.tabs.some((tab) => tab.dirty),
  );

  return (
    <div className="app-frame">
      <header className="app-header">
        <div>
          <p className="app-header__eyebrow">MULTI-AGENT OPERATIONS</p>
          <strong className="app-header__brand">Manyselves</strong>
        </div>
        <ConnectionBanner />
      </header>

      <div className="route-rail" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>

      <div className="workspace-grid">
        <nav className="workspace-pane workspace-pane--files" aria-label="服务器工作区">
          <p className="pane-label">服务器工作区</p>
          <h2>项目与文件</h2>
          {bootstrap && gateway && platform ? (
            <ProjectWorkspace
              activeProjectId={bootstrap.project.id}
              gateway={gateway}
              hasDirtyDrafts={Object.keys(drafts).length > 0 || hasDirtyEditorDrafts}
              key={bootstrap.project.id}
              onOpenFile={(entry) => {
                if (!isEditableTextPath(entry.path)) {
                  setOpenError(null);
                  setPreviewPath(entry.path);
                  return;
                }
                setOpenError(null);
                setPreviewPath(null);
                void editorApi?.read(bootstrap.project.id, entry.path).then(
                  (file) => editorStore.getState().openFile(bootstrap.project.id, file),
                  () => setOpenError("服务器文件读取失败"),
                );
              }}
              onPreviewFile={(entry) => {
                setOpenError(null);
                setPreviewPath(entry.path);
              }}
              onProjectActivated={() => {
                editorStore.getState().reset();
                setPreviewPath(null);
              }}
              platform={platform}
            />
          ) : (
            <>
              <p className="project-identity">{bootstrap?.project.id ?? "等待项目同步"}</p>
              <p className="pane-muted">项目、文件树与导入操作将在此处显示。</p>
            </>
          )}
        </nav>

        <main className="workspace-pane workspace-pane--main" aria-label="主工作区">
          {bootstrap && editorApi && gateway && platform && previewApi && previewPath ? (
            <Suspense fallback={<p role="status">正在加载预览器…</p>}>
              <PreviewWorkspace
                api={previewApi}
                key={previewPath}
                onClose={() => setPreviewPath(null)}
                path={previewPath}
                platform={platform}
                projectId={bootstrap.project.id}
              />
            </Suspense>
          ) : bootstrap && editorApi && gateway && platform ? (
            <Suspense fallback={<p role="status">正在加载编辑器…</p>}>
              <EditorWorkspace
                api={editorApi}
                projectId={bootstrap.project.id}
                serverUrl={gateway.baseUrl}
                store={editorStore}
              />
            </Suspense>
          ) : (
            <>
              <div className="workspace-heading">
                <div>
                  <p className="pane-label">当前草稿</p>
                  <h2>{activeDraftPath}</h2>
                </div>
                <span className="draft-badge">本地</span>
              </div>
              <label className="draft-field">
                <span>本地草稿</span>
                <textarea
                  aria-label="本地草稿"
                  value={activeDraft}
                  onChange={(event) => setDraft(activeDraftPath, event.target.value)}
                />
              </label>
            </>
          )}
          {operationApi && activeEditorPath?.toLowerCase().endsWith(".py") && !previewPath ? (
            <PythonRunAction api={operationApi} path={activeEditorPath} />
          ) : null}
          {openError ? <p role="alert">{openError}</p> : null}
          <section className="conversation-placeholder" aria-label="对话区域">
            <p className="pane-label">对话</p>
            <p>连接 Runtime 后，Agent 消息和操作进度会出现在这里。</p>
          </section>
        </main>

        <aside className="workspace-pane workspace-pane--agents" aria-label="智能体与控制">
          <p className="pane-label">运行状态</p>
          <h2>Agent 与控制</h2>
          <dl className="status-list">
            <div>
              <dt>控制权</dt>
              <dd>观察者</dd>
            </div>
            <div>
              <dt>主 Agent</dt>
              <dd>{bootstrap?.runtime.agent_statuses.main ?? "等待同步"}</dd>
            </div>
          </dl>
        </aside>
      </div>
    </div>
  );
}
