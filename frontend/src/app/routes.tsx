import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect } from "react";
import { Link, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import type { ApiGateway, BootstrapSnapshot } from "../api/gateway";
import { ProjectHomePage } from "../features/projects/ProjectHomePage";
import { createProjectApi, type Project, type ProjectApi } from "../features/projects/project-api";
import { createSettingsApi } from "../features/settings/settings-api";
import type { SettingsStorage } from "../features/settings/settings-storage";
import { AppLayout } from "../features/shell/AppLayout";
import type { PlatformBridge } from "../platform/types";

const ProjectDirectoryPage = lazy(async () => {
  const module = await import("../features/files/ProjectDirectoryPage");
  return { default: module.ProjectDirectoryPage };
});

const GlobalKnowledgePage = lazy(async () => {
  const module = await import("../features/knowledge/GlobalKnowledgePage");
  return { default: module.GlobalKnowledgePage };
});

const ConversationPage = lazy(async () => {
  const module = await import("../features/conversations/ConversationPage");
  return { default: module.ConversationPage };
});

const RuntimeRoutePage = lazy(async () => {
  const module = await import("../features/runtime/RuntimePage");
  return { default: module.RuntimeRoutePage };
});

const LogsRoutePage = lazy(async () => {
  const module = await import("../features/logs/LogsPage");
  return { default: module.LogsRoutePage };
});

const HistoryRoutePage = lazy(async () => {
  const module = await import("../features/history/HistoryPage");
  return { default: module.HistoryRoutePage };
});

const SettingsPage = lazy(async () => {
  const module = await import("../features/settings/SettingsPage");
  return { default: module.SettingsPage };
});

export const lastProjectRouteStorageKey = "manyselves.lastProjectRoute.v1";

function Placeholder({ title }: { readonly title: string }) {
  return <section className="route-placeholder"><h1>{title}</h1><p>此项目区域将在后续工作中提供受控内容。</p></section>;
}

function savedProjectRoute(value: string | null, projects: readonly Project[], bootstrap: BootstrapSnapshot | undefined): string | null {
  if (!value) return null;
  const match = /^\/projects\/([^/?#]+)(?:\/conversations\/([^/?#]+))?$/.exec(value);
  if (!match) return null;
  try {
    const encodedProjectId = match[1];
    if (!encodedProjectId) return null;
    const projectId = decodeURIComponent(encodedProjectId);
    if (!projects.some((project) => project.id === projectId)) return null;
    const encodedConversationId = match[2];
    if (!encodedConversationId) return value;
    const conversationId = decodeURIComponent(encodedConversationId);
    if (
      conversationId === "new"
      || bootstrap?.project.id !== projectId
      || !bootstrap.conversations.some((conversation) => (
        typeof conversation === "object"
        && conversation !== null
        && "sessionId" in conversation
        && conversation.sessionId === conversationId
      ))
    ) return `/projects/${encodedProjectId}`;
    return value;
  } catch {
    return null;
  }
}

function storedRoute(): string | null {
  try { return window.localStorage.getItem(lastProjectRouteStorageKey); } catch { return null; }
}

function persistRoute(pathname: string): void {
  if (!/^\/projects\/[^/?#]+(?:\/conversations\/[^/?#]+)?$/.test(pathname)) return;
  if (pathname.endsWith("/conversations/new")) return;
  try { window.localStorage.setItem(lastProjectRouteStorageKey, pathname); } catch { /* browser storage can be unavailable */ }
}

function RoutePersistence() {
  const location = useLocation();
  useEffect(() => { persistRoute(location.pathname); }, [location.pathname]);
  return null;
}

function ProjectLanding({ gateway, projectApi }: { readonly gateway: ApiGateway; readonly projectApi: ProjectApi }) {
  const savedValue = storedRoute();
  const needsConversationValidation = /\/conversations\/[^/?#]+$/.test(savedValue ?? "");
  const projects = useQuery({ queryFn: () => projectApi.list(), queryKey: ["projects"] });
  const bootstrap = useQuery<BootstrapSnapshot>({
    enabled: needsConversationValidation,
    queryFn: () => gateway.bootstrap(),
    queryKey: ["bootstrap"],
  });
  if (projects.isPending || (needsConversationValidation && bootstrap.isPending)) {
    return <Placeholder title="正在加载项目…" />;
  }
  if (projects.isError) return <section className="route-placeholder"><h1>项目不可用</h1><p aria-live="polite">无法加载项目列表，请稍后重试。</p></section>;
  const saved = savedProjectRoute(savedValue, projects.data, bootstrap.data);
  if (saved) return <Navigate replace to={saved} />;
  const active = projects.data.find((project) => project.active) ?? projects.data[0];
  if (active) return <Navigate replace to={`/projects/${encodeURIComponent(active.id)}`} />;
  return <section className="route-placeholder"><h1>尚无项目</h1><p>请从侧栏新建项目以开始工作。</p></section>;
}

function ProjectRouteLayout({ accountUsername, onLogout, projectApi }: { readonly accountUsername?: string; readonly onLogout?: () => void; readonly projectApi: ProjectApi }) {
  const queryClient = useQueryClient();
  const projects = useQuery({ queryFn: () => projectApi.list(), queryKey: ["projects"] });
  const refreshProjects = async () => { await queryClient.invalidateQueries({ queryKey: ["projects"] }); };
  return <><RoutePersistence /><AppLayout
    {...(accountUsername ? { accountUsername } : {})}
    onCreateProject={async (input) => { const created = await projectApi.create(input); await refreshProjects(); return created; }}
    onDeleteProject={async (projectId) => { await projectApi.delete(projectId); await refreshProjects(); }}
    onUpdateProject={async (projectId, input) => { const updated = await projectApi.update(projectId, input); await refreshProjects(); return updated; }}
    projects={projects.data ?? []}
    projectsError={projects.isError}
    {...(onLogout ? { onLogout } : {})}
  /></>;
}

function RemovedWorkflowRoute() {
  const { projectId } = useParams();
  return projectId
    ? <Navigate replace to={`/projects/${encodeURIComponent(projectId)}`} />
    : <Navigate replace to="/" />;
}

function NotFound() {
  return <section className="route-placeholder"><h1>页面未找到</h1><p>该地址不是可用的工作台路由。</p><Link to="/">返回项目</Link></section>;
}

export function AppRoutes({ accountUsername, gateway, onLogout, platform, settingsStorage }: { readonly accountUsername?: string; readonly gateway: ApiGateway; readonly onLogout?: () => void; readonly platform?: PlatformBridge; readonly settingsStorage?: SettingsStorage }) {
  const projectApi = createProjectApi(gateway);
  const settingsApi = createSettingsApi(gateway);
  return <Routes><Route element={<ProjectRouteLayout projectApi={projectApi} {...(accountUsername ? { accountUsername } : {})} {...(onLogout ? { onLogout } : {})} />}>
    <Route path="/" element={<ProjectLanding gateway={gateway} projectApi={projectApi} />} />
    <Route path="/knowledge" element={(
      <Suspense fallback={<Placeholder title="正在加载全局知识库…" />}>
        <GlobalKnowledgePage gateway={gateway} {...(platform ? { platform } : {})} />
      </Suspense>
    )} />
    <Route path="/projects/:projectId" element={<ProjectHomePage />} />
    <Route path="/projects/:projectId/conversations/:conversationId" element={(
      <Suspense fallback={<Placeholder title="正在加载项目对话…" />}>
        <ConversationPage gateway={gateway} />
      </Suspense>
    )} />
    <Route path="/projects/:projectId/runtime" element={(
      <Suspense fallback={<Placeholder title="正在加载运行态…" />}>
        <RuntimeRoutePage gateway={gateway} />
      </Suspense>
    )} />
    <Route path="/projects/:projectId/logs" element={(
      <Suspense fallback={<Placeholder title="正在加载日志…" />}>
        <LogsRoutePage gateway={gateway} {...(platform ? { platform } : {})} />
      </Suspense>
    )} />
    <Route path="/projects/:projectId/history" element={(
      <Suspense fallback={<Placeholder title="正在加载历史会话…" />}>
        <HistoryRoutePage gateway={gateway} />
      </Suspense>
    )} />
    <Route path="/projects/:projectId/workflows" element={<RemovedWorkflowRoute />} />
    <Route path="/projects/:projectId/:section" element={(
      <Suspense fallback={<Placeholder title="正在加载项目文件…" />}>
        <ProjectDirectoryPage gateway={gateway} {...(platform ? { platform } : {})} />
      </Suspense>
    )} />
    <Route path="/settings/models" element={settingsStorage ? (
      <Suspense fallback={<Placeholder title="正在加载模型设置…" />}>
        <SettingsPage api={settingsApi} storage={settingsStorage} />
      </Suspense>
    ) : <Placeholder title="模型设置" />} />
    <Route path="*" element={<NotFound />} />
  </Route></Routes>;
}
