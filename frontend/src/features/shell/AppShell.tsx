import { lazy, Suspense, useMemo, useState } from "react";
import { useStore } from "zustand";

import { useWorkspaceStore } from "../../app/store-context";
import type { ApiGateway, BootstrapSnapshot } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import { createAgentStore, type AgentStore } from "../agents/agent-store";
import { AgentSidebar } from "../agents/AgentSidebar";
import { createEditorFileApi } from "../editor/editor-api";
import { createEditorStore } from "../editor/editor-store";
import { isEditableTextPath } from "../editor/editable-files";
import type { SelectionInput } from "../editor/selection-context";
import { ConversationWorkspace } from "../conversations/ConversationWorkspace";
import { createOperationApi } from "../preview/operation-api";
import { createPreviewApi } from "../preview/preview-api";
import { PythonRunAction } from "../preview/PythonRunAction";
import { ProjectWorkspace } from "../projects/ProjectWorkspace";
import { createReportingApi } from "../reporting/reporting-api";
import { createReportingStore, type ReportingStore } from "../reporting/reporting-store";
import { ReportingWorkspace } from "../reporting/ReportingWorkspace";
import { createSettingsApi } from "../settings/settings-api";
import {
  createBrowserSettingsStorage,
  type SettingsStorage,
} from "../settings/settings-storage";
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

const SettingsPage = lazy(async () => {
  const module = await import("../settings/SettingsPage");
  return { default: module.SettingsPage };
});

export interface AppShellProps {
  readonly agentStore?: AgentStore;
  readonly bootstrap?: BootstrapSnapshot | undefined;
  readonly gateway?: ApiGateway;
  readonly platform?: PlatformBridge;
  readonly reportingStore?: ReportingStore;
  readonly settingsStorage?: SettingsStorage;
}

export function AppShell({
  agentStore,
  bootstrap,
  gateway,
  platform,
  reportingStore,
  settingsStorage,
}: AppShellProps) {
  const drafts = useWorkspaceStore((store) => store.drafts);
  const setDraft = useWorkspaceStore((store) => store.setDraft);
  const activeDraftPath = useMemo(() => Object.keys(drafts)[0] ?? "scratchpad.md", [drafts]);
  const activeDraft = drafts[activeDraftPath] ?? "";
  const [openError, setOpenError] = useState<string | null>(null);
  const [activeWorkspace, setActiveWorkspace] = useState<"project" | "reporting" | "settings">("project");
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [editorSelection, setEditorSelection] = useState<SelectionInput | null>(null);
  const [projectOverride, setProjectOverride] = useState<{
    readonly sourceProjectId: string;
    readonly targetProjectId: string;
  } | null>(null);
  const editorApi = useMemo(() => gateway ? createEditorFileApi(gateway) : null, [gateway]);
  const operationApi = useMemo(() => gateway ? createOperationApi(gateway) : null, [gateway]);
  const previewApi = useMemo(() => gateway ? createPreviewApi(gateway) : null, [gateway]);
  const reportingApi = useMemo(() => gateway ? createReportingApi(gateway) : null, [gateway]);
  const settingsApi = useMemo(() => gateway ? createSettingsApi(gateway) : null, [gateway]);
  const clientSettings = useMemo(() => settingsStorage ?? createBrowserSettingsStorage({
    localStorage: window.localStorage,
    root: document.documentElement,
    sessionStorage: window.sessionStorage,
  }), [settingsStorage]);
  const [editorStore] = useState(() => createEditorStore());
  const [fallbackAgentStore] = useState(() => createAgentStore(bootstrap?.runtime, bootstrap?.streamId));
  const [fallbackReportingStore] = useState(() => createReportingStore(bootstrap?.streamId ?? null));
  const runtimeStore = agentStore ?? fallbackAgentStore;
  const reports = reportingStore ?? fallbackReportingStore;
  const runtimeState = useStore(runtimeStore);
  const activeEditorPath = useStore(editorStore, (store) => store.activePath);
  const hasDirtyEditorDrafts = useStore(
    editorStore,
    (store) => store.tabs.some((tab) => tab.dirty),
  );

  const bootstrapProjectId = bootstrap?.project.id ?? "";
  const conversationProjectId = projectOverride?.sourceProjectId === bootstrapProjectId
    ? projectOverride.targetProjectId
    : bootstrapProjectId;
  const activeEditorSelection = editorSelection?.path === activeEditorPath ? editorSelection : null;

  return (
    <div className="app-frame">
      <header className="app-header">
        <div>
          <p className="app-header__eyebrow">MULTI-AGENT OPERATIONS</p>
          <strong className="app-header__brand">Manyselves</strong>
        </div>
        <div aria-label="工作区切换" className="app-header__workspace-switch" role="group">
          <button
            aria-pressed={activeWorkspace === "project"}
            onClick={() => setActiveWorkspace("project")}
            type="button"
          >项目工作区</button>
          <button
            aria-pressed={activeWorkspace === "reporting"}
            disabled={!bootstrap || !reportingApi || !platform}
            onClick={() => setActiveWorkspace("reporting")}
            type="button"
          >报告中心</button>
          <button
            aria-pressed={activeWorkspace === "settings"}
            disabled={!bootstrap || !settingsApi}
            onClick={() => setActiveWorkspace("settings")}
            type="button"
          >设置</button>
        </div>
        <ConnectionBanner />
      </header>

      <div className="route-rail" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>

      <div className={`workspace-grid ${activeWorkspace !== "project" ? `workspace-grid--${activeWorkspace}` : ""}`}>
        {activeWorkspace === "project" ? <nav className="workspace-pane workspace-pane--files" aria-label="服务器工作区">
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
              onProjectActivated={(projectId) => {
                editorStore.getState().reset();
                setPreviewPath(null);
                setProjectOverride({
                  sourceProjectId: bootstrap.project.id,
                  targetProjectId: projectId,
                });
              }}
              platform={platform}
            />
          ) : (
            <>
              <p className="project-identity">{bootstrap?.project.id ?? "等待项目同步"}</p>
              <p className="pane-muted">项目、文件树与导入操作将在此处显示。</p>
            </>
          )}
        </nav> : null}

        <main className={`workspace-pane workspace-pane--main ${activeWorkspace !== "project" ? `workspace-pane--${activeWorkspace}` : ""}`} aria-label="主工作区">
          {activeWorkspace === "settings" && settingsApi ? (
            <Suspense fallback={<p role="status">正在加载设置…</p>}>
              <SettingsPage
                api={settingsApi}
                onReconnect={() => window.location.reload()}
                storage={clientSettings}
              />
            </Suspense>
          ) : activeWorkspace === "reporting" && bootstrap && reportingApi && platform ? (
            <ReportingWorkspace
              api={reportingApi}
              platform={platform}
              projectId={bootstrap.project.id}
              store={reports}
            />
          ) : bootstrap && editorApi && gateway && platform && previewApi && previewPath ? (
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
                onSelectionChange={setEditorSelection}
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
          {activeWorkspace === "project" && operationApi && activeEditorPath?.toLowerCase().endsWith(".py") && !previewPath ? (
            <PythonRunAction api={operationApi} path={activeEditorPath} />
          ) : null}
          {openError ? <p role="alert">{openError}</p> : null}
          {activeWorkspace === "project" ? (
            bootstrap && gateway ? (
              <ConversationWorkspace
                agentId="main"
                currentEditorPath={activeEditorPath}
                currentSelection={activeEditorSelection}
                gateway={gateway}
                key={conversationProjectId || bootstrap.project.id}
                liveMessages={Object.values(runtimeState.messages)}
                projectId={conversationProjectId || bootstrap.project.id}
              />
            ) : (
              <section className="conversation-placeholder" aria-label="对话区域">
                <p className="pane-label">对话</p>
                <p>连接 Runtime 后，Agent 消息和操作进度会出现在这里。</p>
              </section>
            )
          ) : null}
        </main>

        <aside className="workspace-pane workspace-pane--agents" aria-label="智能体与控制">
          <AgentSidebar state={runtimeState} />
        </aside>
      </div>
    </div>
  );
}
