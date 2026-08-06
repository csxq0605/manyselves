import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { useParams } from "react-router-dom";

import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";
import { createProjectApi } from "../projects/project-api";
import "./runtime-page.css";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

class RuntimeProjectMismatchError extends Error {}

const copy = {
  activeTasks: "\u8fdb\u884c\u4e2d\u4efb\u52a1",
  agentStatus: "Agent \u72b6\u6001",
  agentStatusMetric: "Agent \u5728\u7ebf\u72b6\u6001",
  cockpit: "\u8fd0\u884c\u9a7e\u9a76\u8231",
  currentTask: "\u5f53\u524d\u4efb\u52a1",
  emptyErrors: "\u5f53\u524d\u6ca1\u6709\u9519\u8bef",
  emptyQueue: "\u961f\u5217\u4e3a\u7a7a",
  emptyTasks: "\u5f53\u524d\u6ca1\u6709\u8fd0\u884c\u4e2d\u7684\u4efb\u52a1",
  emptyTools: "\u6682\u65e0\u5de5\u5177\u8c03\u7528",
  errors: "\u9519\u8bef",
  invalidRuntimeRoute: "\u8fd0\u884c\u6001\u8def\u7531\u65e0\u6548",
  loadingProjects: "\u6b63\u5728\u52a0\u8f7d\u9879\u76ee\u2026",
  loadingRuntime: "\u6b63\u5728\u52a0\u8f7d\u8fd0\u884c\u6001\u2026",
  projectActivationFailed: "\u9879\u76ee\u5207\u6362\u5931\u8d25",
  projectMismatch: "\u8fd0\u884c\u6001\u9879\u76ee\u6821\u9a8c\u5931\u8d25",
  projectSwitching: "\u6b63\u5728\u5207\u6362\u9879\u76ee\u2026",
  projectUnavailable: "\u9879\u76ee\u4e0d\u53ef\u7528",
  queues: "\u7b49\u5f85\u961f\u5217",
  ready: "\u8fd0\u884c\u670d\u52a1\u5c31\u7eea",
  runtimeLoadFailed: "\u8fd0\u884c\u6001\u52a0\u8f7d\u5931\u8d25",
  taskCompletion: "\u4efb\u52a1\u5b8c\u6210\u5ea6",
  title: "\u8fd0\u884c\u6001",
  toolAndErrors: "\u5de5\u5177\u4e0e\u9519\u8bef",
  tools: "\u5de5\u5177\u8c03\u7528",
  toolsRunning: "\u4e2a\u5de5\u5177\u8c03\u7528\u4e2d",
  offline: "\u8fd0\u884c\u670d\u52a1\u672a\u5c31\u7eea",
};

export interface RuntimePageProps {
  readonly snapshot: RuntimeSnapshot;
}

function displayError(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value;
  return "\u6267\u884c\u5931\u8d25";
}

function isComplete(status: string): boolean {
  return ["completed", "done", "success", "succeeded"].includes(status.toLowerCase());
}

function statusWithBlocking(status: string, blocking: boolean): string {
  return blocking ? `${status} · \u963b\u585e` : status;
}

export function RuntimePage({ snapshot }: RuntimePageProps) {
  const activeTasks = snapshot.tasks.filter((task) => !isComplete(task.status));
  const runningTools = snapshot.tools.filter((tool) => tool.status === "running");
  const failedTools = snapshot.tools.filter((tool) => tool.status === "failed");
  const debugErrors = snapshot.debug.filter((entry) => entry.status.toLowerCase() === "error");
  const completedTasks = snapshot.tasks.length - activeTasks.length;
  const agentCount = Object.keys(snapshot.agent_statuses).length;
  const errorCount = failedTools.length + debugErrors.length;

  return (
    <section className="runtime-page">
      <header className="operations-header runtime-page__hero">
        <div>
          <p>{copy.cockpit}</p>
          <h1>{copy.title}</h1>
        </div>
        <span className={`runtime-page__readiness runtime-page__readiness--${snapshot.ready ? "ready" : "offline"}`}>
          {snapshot.ready ? copy.ready : copy.offline}
        </span>
      </header>

      <div className="runtime-page__summary" aria-label={"\u8fd0\u884c\u603b\u89c8"}>
        <article className="runtime-page__metric">
          <span>{copy.activeTasks}</span>
          <strong>{activeTasks.length}</strong>
          <small>{activeTasks.length} {"\u4e2a\u4efb\u52a1\u8fdb\u884c\u4e2d"}</small>
        </article>
        <article className="runtime-page__metric">
          <span>{copy.taskCompletion}</span>
          <strong>{completedTasks} / {snapshot.tasks.length}</strong>
        </article>
        <article className="runtime-page__metric">
          <span>{copy.agentStatusMetric}</span>
          <strong>{agentCount}</strong>
        </article>
        <article className="runtime-page__metric">
          <span>{copy.toolAndErrors}</span>
          <strong>{runningTools.length} / {errorCount}</strong>
        </article>
      </div>

      <div className="runtime-page__grid">
        <section className="operations-card operations-card--focus" aria-labelledby="runtime-current-task">
          <h2 id="runtime-current-task">{copy.currentTask}</h2>
          {activeTasks.length === 0 ? <p className="operations-empty">{copy.emptyTasks}</p> : (
            <ul className="operations-list">
              {activeTasks.map((task) => <li key={task.task_id}>
                <strong>{task.brief || task.task_id}</strong>
                <span>{task.source_agent} {"\u2192"} {task.target_agent}</span>
                <small>{statusWithBlocking(task.status, task.blocking)}</small>
              </li>)}
            </ul>
          )}
        </section>

        <section className="operations-card" aria-labelledby="runtime-agents">
          <h2 id="runtime-agents">{copy.agentStatus}</h2>
          <ul className="operations-list operations-list--compact">
            {Object.entries(snapshot.agent_statuses).map(([agentId, status]) => <li key={agentId}>
              <span className={`runtime-page__signal runtime-page__signal--${status}`} aria-hidden="true" />
              <strong>{agentId}</strong><span>{status}</span>
            </li>)}
          </ul>
        </section>

        <section className="operations-card" aria-labelledby="runtime-queues">
          <h2 id="runtime-queues">{copy.queues}</h2>
          {snapshot.queues.every((queue) => queue.pending_count === 0) ? <p className="operations-empty">{copy.emptyQueue}</p> : (
            <ul className="operations-list">
              {snapshot.queues.filter((queue) => queue.pending_count > 0).map((queue) => <li key={queue.agent_id}>
                <strong>{queue.agent_id} · {queue.pending_count}</strong>
                <span>{queue.queued_messages.join(" · ")}</span>
              </li>)}
            </ul>
          )}
        </section>

        <section className="operations-card" aria-labelledby="runtime-tools">
          <h2 id="runtime-tools">{copy.tools}</h2>
          {snapshot.tools.length === 0 ? <p className="operations-empty">{copy.emptyTools}</p> : (
            <ul className="operations-list">
              {snapshot.tools.map((tool) => <li key={tool.toolCallId}>
                <strong>{tool.name}</strong><span>{tool.agentId}</span><small>{tool.status}</small>
              </li>)}
            </ul>
          )}
        </section>
      </div>

      <section className="operations-card runtime-page__errors" aria-labelledby="runtime-errors">
        <h2 id="runtime-errors">{copy.errors}</h2>
        {failedTools.length === 0 && debugErrors.length === 0 ? <p className="operations-empty">{copy.emptyErrors}</p> : (
          <ul className="operations-list">
            {failedTools.map((tool) => <li key={tool.toolCallId}><strong>{tool.name}：{displayError(tool.error)}</strong><span>{tool.agentId}</span></li>)}
            {debugErrors.map((entry, index) => <li key={`${entry.timestamp}-${index}`}><strong>{entry.model || "\u6a21\u578b\u8c03\u7528"}：\u8c03\u7528\u5931\u8d25</strong><span>{entry.agentId}</span></li>)}
          </ul>
        )}
      </section>
    </section>
  );
}

export function RuntimeRoutePage({ gateway }: { readonly gateway: ApiGateway }) {
  const { projectId } = useParams();
  const queryClient = useQueryClient();
  const projectApi = useMemo(() => createProjectApi(gateway), [gateway]);
  const projects = useQuery({ queryFn: () => projectApi.list(), queryKey: ["projects"] });
  const routeProject = projects.data?.find((project) => project.id === projectId);
  const activation = useQuery({
    enabled: Boolean(projectId && routeProject && !routeProject.active),
    queryFn: async () => {
      const activated = await projectApi.activate(projectId!);
      queryClient.setQueryData<Awaited<ReturnType<typeof projectApi.list>>>(["projects"], (current) => (
        current?.map((project) => ({ ...project, active: project.id === activated.id }))
      ));
      queryClient.removeQueries({ queryKey: ["conversations"] });
      queryClient.removeQueries({ queryKey: ["conversation-messages"] });
      const savedState = localStorage.getItem("manyselves-active-conversation");
      if (savedState) {
        try {
          const parsed = JSON.parse(savedState);
          if (parsed.state?.activeSessionIds) {
            const currentProjectSessionId = parsed.state.activeSessionIds[projectId!];
            parsed.state.activeSessionIds = currentProjectSessionId
              ? { [projectId!]: currentProjectSessionId }
              : {};
            localStorage.setItem("manyselves-active-conversation", JSON.stringify(parsed));
          }
        } catch {
          localStorage.removeItem("manyselves-active-conversation");
        }
      }
      return activated;
    },
    queryKey: ["project-activation", projectId, routeProject?.revision],
    retry: false,
  });
  const projectReady = Boolean(routeProject?.active || activation.isSuccess);
  const runtime = useQuery({
    enabled: projectReady,
    queryFn: async () => {
      const snapshot = await gateway.bootstrap();
      if (snapshot.project.id !== projectId) throw new RuntimeProjectMismatchError();
      return snapshot.runtime;
    },
    queryKey: ["runtime", projectId],
  });

  if (!projectId) return <p role="alert">{copy.invalidRuntimeRoute}</p>;
  if (projects.isPending) return <p role="status">{copy.loadingProjects}</p>;
  if (projects.isError || !routeProject) return <p role="alert">{copy.projectUnavailable}</p>;
  if (!projectReady) return activation.isError ? <p role="alert">{copy.projectActivationFailed}</p> : <p role="status">{copy.projectSwitching}</p>;
  if (runtime.isPending) return <p role="status">{copy.loadingRuntime}</p>;
  if (runtime.isError) return (
    <p role="alert">
      {runtime.error instanceof RuntimeProjectMismatchError ? copy.projectMismatch : copy.runtimeLoadFailed}
    </p>
  );
  return <RuntimePage snapshot={runtime.data} />;
}
