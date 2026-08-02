import { useMemo } from "react";

import { useWorkspaceStore } from "../../app/store-context";
import type { ApiGateway, BootstrapSnapshot } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import { ProjectWorkspace } from "../projects/ProjectWorkspace";
import { ConnectionBanner } from "./ConnectionBanner";
import "./app-shell.css";

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
              hasDirtyDrafts={Object.keys(drafts).length > 0}
              key={bootstrap.project.id}
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
