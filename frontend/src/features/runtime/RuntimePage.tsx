import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { useParams } from "react-router-dom";

import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";
import { createProjectApi } from "../projects/project-api";
import { shortAgentLabel } from "../agents/agent-label";
import {
  reportingRunStage,
  reportingRunStepSummary,
  reportingStepText,
  reportingWorkflowLabel,
} from "../reporting/reporting-run-presentation";
import {
  createWorkflowApi,
  fetchRunProgress,
  type RunProgressResponse,
  type WorkflowRunResponse,
} from "../runs/workflow-api";
import { RunProgressEvents, RunProgressPanel } from "./RunProgressPanel";
import { focusRun, isActiveRun } from "./run-focus";
import "./runtime-page.css";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

class RuntimeProjectMismatchError extends Error {}

const LIVE_REFRESH_MS = 2_500;
const IDLE_REFRESH_MS = 10_000;

const copy = {
  activeRuns: "进行中运行",
  agentStatus: "Agent \u72b6\u6001",
  agentStatusMetric: "Agent \u5728\u7ebf\u72b6\u6001",
  cockpit: "\u8fd0\u884c\u9a7e\u9a76\u8231",
  collaborationTasks: "对话协作任务",
  declarativeRuns: "声明式运行",
  emptyAgents: "当前没有 Agent",
  emptyErrors: "\u5f53\u524d\u6ca1\u6709\u9519\u8bef",
  emptyQueue: "\u961f\u5217\u4e3a\u7a7a",
  emptyRuns: "当前没有声明式运行",
  emptyTasks: "当前没有对话协作任务",
  emptyTools: "\u6682\u65e0\u5de5\u5177\u8c03\u7528",
  errors: "\u9519\u8bef",
  invalidRuntimeRoute: "\u8fd0\u884c\u6001\u8def\u7531\u65e0\u6548",
  loadingProgress: "正在读取运行进度…",
  newestFirst: "最新在上",
  progress: "运行进度",
  recentEvents: "运行动态（最新在上）",
  loadingProjects: "\u6b63\u5728\u52a0\u8f7d\u9879\u76ee\u2026",
  loadingRuntime: "\u6b63\u5728\u52a0\u8f7d\u8fd0\u884c\u6001\u2026",
  projectActivationFailed: "\u9879\u76ee\u5207\u6362\u5931\u8d25",
  projectMismatch: "\u8fd0\u884c\u6001\u9879\u76ee\u6821\u9a8c\u5931\u8d25",
  projectSwitching: "\u6b63\u5728\u5207\u6362\u9879\u76ee\u2026",
  projectUnavailable: "\u9879\u76ee\u4e0d\u53ef\u7528",
  queues: "\u7b49\u5f85\u961f\u5217",
  ready: "\u8fd0\u884c\u670d\u52a1\u5c31\u7eea",
  runtimeLoadFailed: "\u8fd0\u884c\u6001\u52a0\u8f7d\u5931\u8d25",
  runCompletion: "运行完成度",
  title: "\u8fd0\u884c\u6001",
  toolAndErrors: "\u5de5\u5177\u4e0e\u9519\u8bef",
  tools: "\u5de5\u5177\u8c03\u7528",
  toolsRunning: "\u4e2a\u5de5\u5177\u8c03\u7528\u4e2d",
  offline: "\u8fd0\u884c\u670d\u52a1\u672a\u5c31\u7eea",
};

export interface RuntimePageProps {
  /** Where the focused Run is now; null while it loads or when there is no Run. */
  readonly progress?: RunProgressResponse | null;
  readonly runs?: readonly WorkflowRunResponse[];
  readonly snapshot: RuntimeSnapshot;
}

const MAX_ROWS = 60;

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

const agentStatusText: Readonly<Record<string, string>> = {
  error: "出错",
  idle: "空闲",
  running: "运行中",
  running_tool: "调用工具",
  thinking: "思考中",
  waiting: "等待",
};

export function RuntimePage({ progress = null, runs = [], snapshot }: RuntimePageProps) {
  const activeTasks = snapshot.tasks.filter((task) => !isComplete(task.status));
  const activeRuns = runs.filter(isActiveRun);
  const completedRuns = runs.filter((run) => isComplete(run.run.status));
  // Newest first: the projection keeps calls in the order they started.
  const tools = [...snapshot.tools].reverse();
  const runningTools = tools.filter((tool) => tool.status === "running");
  const failedTools = tools.filter((tool) => tool.status === "failed");
  const debugErrors = [...snapshot.debug].reverse().filter((entry) => entry.status.toLowerCase() === "error");
  const runErrors = (progress?.events ?? []).filter((event) => (
    event.kind === "map.item.failed" || event.kind === "action.failed"
  ));
  const agents = Object.entries(snapshot.agent_statuses);
  const busyAgents = agents.filter(([, status]) => status !== "idle");
  const pendingQueues = snapshot.queues.filter((queue) => queue.pending_count > 0);
  const errorCount = failedTools.length + debugErrors.length + runErrors.length;
  const focused = focusRun(runs);

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
          <span>{copy.activeRuns}</span>
          <strong>{activeRuns.length}</strong>
          <small>{activeRuns.length} 个运行进行中</small>
        </article>
        <article className="runtime-page__metric">
          <span>{copy.runCompletion}</span>
          <strong>{completedRuns.length} / {runs.length}</strong>
        </article>
        <article className="runtime-page__metric">
          <span>{copy.agentStatusMetric}</span>
          <strong>{busyAgents.length}</strong>
          <small>{busyAgents.length} 个 Agent 正在工作</small>
        </article>
        <article className="runtime-page__metric">
          <span>{copy.toolAndErrors}</span>
          <strong>{runningTools.length} / {errorCount}</strong>
        </article>
      </div>

      {focused ? (
        <section className="operations-card runtime-page__progress" aria-labelledby="runtime-progress">
          <div className="operations-card__head">
            <h2 id="runtime-progress">{copy.progress}</h2>
            <small>{reportingWorkflowLabel(focused.run.workflowId)} · {focused.run.status}</small>
          </div>
          <div className="runtime-page__progress-body">
            <div className="runtime-page__progress-tree">
              {progress && progress.runId === focused.run.runId
                ? <RunProgressPanel progress={progress} />
                : <p className="operations-empty">{copy.loadingProgress}</p>}
            </div>
            <div className="runtime-page__progress-events">
              <h3>{copy.recentEvents}</h3>
              <RunProgressEvents events={progress?.runId === focused.run.runId ? progress.events : []} />
            </div>
          </div>
        </section>
      ) : null}

      <section className="operations-card runtime-page__runs" aria-labelledby="runtime-workflow-runs">
        <h2 id="runtime-workflow-runs">{copy.declarativeRuns}</h2>
        {runs.length === 0 ? <p className="operations-empty">{copy.emptyRuns}</p> : (
          <ul className="runtime-run-list">
            {runs.map((run) => <li key={run.run.runId}>
              <div className="runtime-run-list__heading">
                <strong>{reportingWorkflowLabel(run.run.workflowId)}</strong>
                <span className={`runtime-run-list__status runtime-run-list__status--${run.run.status}`}>
                  {run.run.status}
                </span>
              </div>
              <span className="runtime-run-list__stage">{reportingRunStage(run)}</span>
              <small>{reportingRunStepSummary(run)}</small>
              <code>{run.run.runId}</code>
            </li>)}
          </ul>
        )}
      </section>

      <div className="runtime-page__grid">
        <section className="operations-card" aria-labelledby="runtime-agents">
          <h2 id="runtime-agents">{copy.agentStatus}</h2>
          {agents.length === 0 ? <p className="operations-empty">{copy.emptyAgents}</p> : (
            <ul className="operations-list operations-list--compact">
              {agents.map(([agentId, status]) => <li key={agentId} title={agentId}>
                <span className={`runtime-page__signal runtime-page__signal--${status}`} aria-hidden="true" />
                <strong>{shortAgentLabel(agentId)}</strong><span>{agentStatusText[status] ?? status}</span>
              </li>)}
            </ul>
          )}
        </section>

        <section className="operations-card operations-card--tools" aria-labelledby="runtime-tools">
          <div className="operations-card__head">
            <h2 id="runtime-tools">{copy.tools}</h2>
            <small>{copy.newestFirst}</small>
          </div>
          {tools.length === 0 ? <p className="operations-empty">{copy.emptyTools}</p> : (
            <ul className="operations-list">
              {tools.slice(0, MAX_ROWS).map((tool) => <li key={tool.toolCallId} title={tool.agentId}>
                <strong>{tool.name}</strong>
                <span>{shortAgentLabel(tool.agentId)}</span>
                <small className={`runtime-page__tool-status runtime-page__tool-status--${tool.status}`}>{tool.status}</small>
              </li>)}
            </ul>
          )}
        </section>

        {activeTasks.length > 0 ? (
          <section className="operations-card operations-card--focus" aria-labelledby="runtime-current-task">
            <h2 id="runtime-current-task">{copy.collaborationTasks}</h2>
            <ul className="operations-list">
              {activeTasks.map((task) => <li key={task.task_id}>
                <strong>{task.brief || task.task_id}</strong>
                <span>{task.source_agent} {"\u2192"} {task.target_agent}</span>
                <small>{statusWithBlocking(task.status, task.blocking)}</small>
              </li>)}
            </ul>
          </section>
        ) : null}

        {pendingQueues.length > 0 ? (
          <section className="operations-card" aria-labelledby="runtime-queues">
            <h2 id="runtime-queues">{copy.queues}</h2>
            <ul className="operations-list">
              {pendingQueues.map((queue) => <li key={queue.agent_id}>
                <strong>{shortAgentLabel(queue.agent_id)} · {queue.pending_count}</strong>
                <span>{queue.queued_messages.join(" · ")}</span>
              </li>)}
            </ul>
          </section>
        ) : null}
      </div>

      <section className="operations-card runtime-page__errors" aria-labelledby="runtime-errors">
        <div className="operations-card__head">
          <h2 id="runtime-errors">{copy.errors}</h2>
          <small>{copy.newestFirst}</small>
        </div>
        {errorCount === 0 ? <p className="operations-empty">{copy.emptyErrors}</p> : (
          <ul className="operations-list">
            {runErrors.map((event, index) => <li key={`run-${event.ts ?? ""}-${index}`}>
              <strong>{reportingStepText(event.action_id ?? "").label}{event.key ? ` · ${event.key}` : ""}：{event.error ?? "失败"}</strong>
              <span>{reportingWorkflowLabel(event.workflow_id)}</span>
            </li>)}
            {failedTools.map((tool) => <li key={tool.toolCallId}><strong>{tool.name}：{displayError(tool.error)}</strong><span>{shortAgentLabel(tool.agentId)}</span></li>)}
            {debugErrors.map((entry, index) => <li key={`${entry.timestamp}-${index}`}><strong>{entry.model || "\u6a21\u578b\u8c03\u7528"}：调用失败</strong><span>{shortAgentLabel(entry.agentId)}</span></li>)}
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
  const workflowApi = useMemo(() => createWorkflowApi(gateway), [gateway]);
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
  const runs = useQuery({
    enabled: projectReady,
    queryFn: () => workflowApi.listRuns(),
    queryKey: ["runs", "runtime", projectId],
    refetchInterval: (query) => (
      query.state.data?.runs.some(isActiveRun) ? LIVE_REFRESH_MS : IDLE_REFRESH_MS
    ),
  });
  const running = Boolean(runs.data?.runs.some(isActiveRun));
  const runtime = useQuery({
    enabled: projectReady,
    queryFn: async () => {
      const snapshot = await gateway.bootstrap();
      if (snapshot.project.id !== projectId) throw new RuntimeProjectMismatchError();
      return snapshot.runtime;
    },
    queryKey: ["runtime", projectId],
    // Workflow Agents report through events the snapshot keeps; read it
    // again while a Run works so new calls and errors show up as they happen.
    refetchInterval: running ? LIVE_REFRESH_MS : false,
  });
  const focused = runs.data ? focusRun(runs.data.runs) : undefined;
  const progress = useQuery({
    enabled: projectReady && Boolean(focused),
    queryFn: () => fetchRunProgress(gateway, focused!.run.runId),
    queryKey: ["runs", focused?.run.runId ?? "", "progress"],
    refetchInterval: focused && isActiveRun(focused) ? LIVE_REFRESH_MS : false,
  });

  if (!projectId) return <p role="alert">{copy.invalidRuntimeRoute}</p>;
  if (projects.isPending) return <p role="status">{copy.loadingProjects}</p>;
  if (projects.isError || !routeProject) return <p role="alert">{copy.projectUnavailable}</p>;
  if (!projectReady) return activation.isError ? <p role="alert">{copy.projectActivationFailed}</p> : <p role="status">{copy.projectSwitching}</p>;
  if (runtime.isPending || runs.isPending) return <p role="status">{copy.loadingRuntime}</p>;
  if (runtime.isError || runs.isError) return (
    <p role="alert">
      {runtime.error instanceof RuntimeProjectMismatchError ? copy.projectMismatch : copy.runtimeLoadFailed}
    </p>
  );
  return <RuntimePage progress={progress.data ?? null} runs={runs.data.runs} snapshot={runtime.data} />;
}
