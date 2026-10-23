import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { useParams } from "react-router-dom";

import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";
import { createProjectApi } from "../projects/project-api";
import { shortAgentLabel } from "../agents/agent-label";
import {
  reportingStages,
  reportingStepText,
  reportingWorkflowLabel,
  type ReportingStage,
} from "../reporting/reporting-run-presentation";
import {
  createWorkflowApi,
  fetchRunProgress,
  type RunProgressResponse,
  type WorkflowRunResponse,
} from "../runs/workflow-api";
import { RunProgressEvents, RunProgressPanel } from "./RunProgressPanel";
import { focusRun, isActiveRun } from "./run-focus";
import {
  currentLine,
  formatElapsed,
  runElapsed,
  runStages,
  type StageStatus,
} from "./run-stages";
import "./runtime-page.css";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

class RuntimeProjectMismatchError extends Error {}

const LIVE_REFRESH_MS = 2_500;
const IDLE_REFRESH_MS = 10_000;

const copy = {
  agentStatus: "Agent 状态",
  details: "内部步骤与运行明细",
  emptyRuns: "当前没有运行",
  emptyTools: "暂无工具调用",
  innerSteps: "内部步骤",
  invalidRuntimeRoute: "运行态路由无效",
  latestErrors: "最新错误",
  loadingProgress: "正在读取运行进度…",
  loadingProjects: "正在加载项目…",
  loadingRuntime: "正在加载运行态…",
  offline: "运行服务未就绪",
  projectActivationFailed: "项目切换失败",
  projectMismatch: "运行态项目校验失败",
  projectSwitching: "正在切换项目…",
  projectUnavailable: "项目不可用",
  ready: "运行服务就绪",
  recentEvents: "运行动态（最新在上）",
  runtimeLoadFailed: "运行态加载失败",
  title: "运行态",
  tools: "工具调用（最新在上）",
};

export interface RuntimePageProps {
  /** Where the focused Run is now; null while it loads or when there is no Run. */
  readonly progress?: RunProgressResponse | null;
  readonly runs?: readonly WorkflowRunResponse[];
  readonly snapshot: RuntimeSnapshot;
}

const MAX_ROWS = 60;
const MAX_ERRORS = 3;

function displayError(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value;
  return "执行失败";
}

const agentStatusText: Readonly<Record<string, string>> = {
  error: "出错",
  idle: "空闲",
  running: "运行中",
  running_tool: "调用工具",
  thinking: "思考中",
  waiting: "等待",
};

const stageStatusText: Readonly<Record<StageStatus, string>> = {
  completed: "完成",
  failed: "失败",
  pending: "等待",
  running: "进行中",
  waiting: "等待输入",
};

const runStatusText: Readonly<Record<string, string>> = {
  completed: "已完成",
  failed: "失败",
  running: "运行中",
  waiting: "等待输入",
};

interface ErrorRow {
  readonly key: string;
  readonly text: string;
  readonly time: string | null;
}

function timeOf(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleTimeString("zh-CN", { hour12: false });
}

/** The stage list for this Run: its own, or its top-level steps one per line. */
function stagesFor(progress: RunProgressResponse): readonly ReportingStage[] {
  const own = reportingStages(progress.workflowId);
  if (own) return own;
  const root = progress.root;
  return (root?.steps ?? []).map((step) => ({
    label: reportingStepText(step.id).label,
    steps: [`${root!.workflow_id}/${step.id}`],
  }));
}

function RunStagesCard({ progress, run }: { readonly progress: RunProgressResponse | null; readonly run: WorkflowRunResponse }) {
  const running = isActiveRun(run);
  const stages = progress ? runStages(progress, stagesFor(progress)) : [];
  const line = progress && running ? currentLine(progress) : null;
  const total = progress ? runElapsed(progress, running) : null;
  return (
    <section className="operations-card run-stages" aria-labelledby="runtime-progress">
      <div className="run-stages__head">
        <h2 id="runtime-progress">{reportingWorkflowLabel(run.run.workflowId)}</h2>
        <span className={`run-stages__pill run-stages__pill--${run.run.status}`}>
          {runStatusText[run.run.status] ?? run.run.status}
        </span>
        <small>{total !== null ? `已用 ${formatElapsed(total)}` : ""}</small>
      </div>
      {!progress ? <p className="operations-empty">{copy.loadingProgress}</p> : (
        <ol aria-label="运行阶段" className="run-stages__list">
          {stages.map((stage) => (
            <li className={`run-stages__stage run-stages__stage--${stage.status}`} key={stage.label}>
              <span aria-hidden="true" className="run-stages__dot" />
              <span className="run-stages__label">{stage.label}</span>
              <span className="run-stages__status">{stageStatusText[stage.status]}</span>
              <span className="run-stages__time">{formatElapsed(stage.elapsedMs)}</span>
              {line && (stage.status === "running" || stage.status === "waiting")
                ? <span className="run-stages__line">{line}</span>
                : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function RuntimePage({ progress = null, runs = [], snapshot }: RuntimePageProps) {
  // Newest first: the projection keeps calls in the order they started.
  const tools = [...snapshot.tools].reverse();
  const agents = Object.entries(snapshot.agent_statuses);
  const busyAgents = agents.filter(([, status]) => status !== "idle");
  const focused = focusRun(runs);
  const current = progress && focused && progress.runId === focused.run.runId ? progress : null;
  const errors: ErrorRow[] = [
    ...(current?.events ?? [])
      .filter((event) => event.kind === "map.item.failed" || event.kind === "action.failed")
      .map((event, index) => ({
        key: `run-${index}`,
        text: `${reportingStepText(event.action_id ?? "").label}${event.key ? ` · ${event.key}` : ""}：${event.error ?? "失败"}`,
        time: event.ts,
      })),
    ...tools.filter((tool) => tool.status === "failed").map((tool) => ({
      key: `tool-${tool.toolCallId}`,
      text: `${tool.name}：${displayError(tool.error)}（${shortAgentLabel(tool.agentId)}）`,
      time: null,
    })),
    ...[...snapshot.debug].reverse().filter((entry) => entry.status.toLowerCase() === "error").map((entry, index) => ({
      key: `debug-${index}`,
      text: `${entry.model || "模型调用"}：调用失败（${shortAgentLabel(entry.agentId)}）`,
      time: entry.timestamp,
    })),
  ].slice(0, MAX_ERRORS);

  return (
    <section className="runtime-page">
      <header className="runtime-page__title">
        <h1>{copy.title}</h1>
        <span className={`runtime-page__readiness runtime-page__readiness--${snapshot.ready ? "ready" : "offline"}`}>
          {snapshot.ready ? copy.ready : copy.offline}
        </span>
      </header>

      {focused ? <RunStagesCard progress={current} run={focused} /> : (
        <section className="operations-card"><p className="operations-empty">{copy.emptyRuns}</p></section>
      )}

      {errors.length > 0 ? (
        <section className="operations-card runtime-page__errors" aria-labelledby="runtime-errors">
          <h2 id="runtime-errors">{copy.latestErrors}</h2>
          <ul className="operations-list">
            {errors.map((row) => <li key={row.key}>
              <strong>{row.text}</strong>
              <span>{timeOf(row.time)}</span>
            </li>)}
          </ul>
        </section>
      ) : null}

      <details className="operations-card runtime-page__more">
        <summary>{copy.details}<small>{busyAgents.length} 个 Agent 在工作</small></summary>
        <div className="runtime-page__more-body">
          {current ? (
            <div className="runtime-page__progress-body">
              <div className="runtime-page__progress-tree">
                <h3>{copy.innerSteps}</h3>
                <RunProgressPanel progress={current} />
              </div>
              <div className="runtime-page__progress-events">
                <h3>{copy.recentEvents}</h3>
                <RunProgressEvents events={current.events} />
              </div>
            </div>
          ) : null}
          <div className="runtime-page__grid">
            <section aria-labelledby="runtime-agents">
              <h3 id="runtime-agents">{copy.agentStatus}</h3>
              <ul className="operations-list operations-list--compact">
                {agents.map(([agentId, status]) => <li key={agentId} title={agentId}>
                  <span className={`runtime-page__signal runtime-page__signal--${status}`} aria-hidden="true" />
                  <strong>{shortAgentLabel(agentId)}</strong><span>{agentStatusText[status] ?? status}</span>
                </li>)}
              </ul>
            </section>
            <section aria-labelledby="runtime-tools">
              <h3 id="runtime-tools">{copy.tools}</h3>
              {tools.length === 0 ? <p className="operations-empty">{copy.emptyTools}</p> : (
                <ul className="operations-list">
                  {tools.slice(0, MAX_ROWS).map((tool) => <li key={tool.toolCallId} title={tool.agentId}>
                    <strong>{tool.name}</strong>
                    <span>{shortAgentLabel(tool.agentId)} · {tool.status}</span>
                  </li>)}
                </ul>
              )}
            </section>
          </div>
        </div>
      </details>
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
