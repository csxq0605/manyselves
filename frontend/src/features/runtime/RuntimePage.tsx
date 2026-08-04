import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { useParams } from "react-router-dom";

import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";
import { createProjectApi } from "../projects/project-api";
import "./runtime-page.css";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

class RuntimeProjectMismatchError extends Error {}

export interface RuntimePageProps {
  readonly snapshot: RuntimeSnapshot;
}

function displayError(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value;
  return "执行失败";
}

function isComplete(status: string): boolean {
  return ["completed", "done", "success", "succeeded"].includes(status.toLowerCase());
}

export function RuntimePage({ snapshot }: RuntimePageProps) {
  const activeTasks = snapshot.tasks.filter((task) => !isComplete(task.status));
  const failedTools = snapshot.tools.filter((tool) => tool.status === "failed");
  const debugErrors = snapshot.debug.filter((entry) => entry.status.toLowerCase() === "error");
  const completedTasks = snapshot.tasks.length - activeTasks.length;

  return (
    <section className="runtime-page">
      <header className="operations-header">
        <div><p>PROJECT RUNTIME</p><h1>运行态</h1></div>
        <span className={`runtime-page__readiness runtime-page__readiness--${snapshot.ready ? "ready" : "offline"}`}>
          {snapshot.ready ? "运行服务就绪" : "运行服务未就绪"}
        </span>
      </header>

      <div className="runtime-page__summary" aria-label="运行进度">
        <strong>{activeTasks.length} 个任务进行中</strong>
        <span>{completedTasks} / {snapshot.tasks.length} 已完成</span>
        <span>{snapshot.tools.filter((tool) => tool.status === "running").length} 个工具调用中</span>
      </div>

      <div className="runtime-page__grid">
        <section className="operations-card" aria-labelledby="runtime-current-task">
          <h2 id="runtime-current-task">当前任务</h2>
          {activeTasks.length === 0 ? <p className="operations-empty">当前没有运行中的任务</p> : (
            <ul className="operations-list">
              {activeTasks.map((task) => <li key={task.task_id}>
                <strong>{task.brief || task.task_id}</strong>
                <span>{task.source_agent} → {task.target_agent}</span>
                <small>{task.status}{task.blocking ? " · 阻塞" : ""}</small>
              </li>)}
            </ul>
          )}
        </section>

        <section className="operations-card" aria-labelledby="runtime-agents">
          <h2 id="runtime-agents">Agent 状态</h2>
          <ul className="operations-list operations-list--compact">
            {Object.entries(snapshot.agent_statuses).map(([agentId, status]) => <li key={agentId}>
              <span className={`runtime-page__signal runtime-page__signal--${status}`} aria-hidden="true" />
              <strong>{agentId}</strong><span>{status}</span>
            </li>)}
          </ul>
        </section>

        <section className="operations-card" aria-labelledby="runtime-queues">
          <h2 id="runtime-queues">等待队列</h2>
          {snapshot.queues.every((queue) => queue.pending_count === 0) ? <p className="operations-empty">队列为空</p> : (
            <ul className="operations-list">
              {snapshot.queues.filter((queue) => queue.pending_count > 0).map((queue) => <li key={queue.agent_id}>
                <strong>{queue.agent_id} · {queue.pending_count}</strong>
                <span>{queue.queued_messages.join(" · ")}</span>
              </li>)}
            </ul>
          )}
        </section>

        <section className="operations-card" aria-labelledby="runtime-tools">
          <h2 id="runtime-tools">工具调用</h2>
          {snapshot.tools.length === 0 ? <p className="operations-empty">暂无工具调用</p> : (
            <ul className="operations-list">
              {snapshot.tools.map((tool) => <li key={tool.toolCallId}>
                <strong>{tool.name}</strong><span>{tool.agentId}</span><small>{tool.status}</small>
              </li>)}
            </ul>
          )}
        </section>
      </div>

      <section className="operations-card runtime-page__errors" aria-labelledby="runtime-errors">
        <h2 id="runtime-errors">错误</h2>
        {failedTools.length === 0 && debugErrors.length === 0 ? <p className="operations-empty">当前没有错误</p> : (
          <ul className="operations-list">
            {failedTools.map((tool) => <li key={tool.toolCallId}><strong>{tool.name}：{displayError(tool.error)}</strong><span>{tool.agentId}</span></li>)}
            {debugErrors.map((entry, index) => <li key={`${entry.timestamp}-${index}`}><strong>{entry.model || "模型调用"}：调用失败</strong><span>{entry.agentId}</span></li>)}
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

  if (!projectId) return <p role="alert">运行态路由无效</p>;
  if (projects.isPending) return <p role="status">正在加载项目…</p>;
  if (projects.isError || !routeProject) return <p role="alert">项目不可用</p>;
  if (!projectReady) return activation.isError ? <p role="alert">项目切换失败</p> : <p role="status">正在切换项目…</p>;
  if (runtime.isPending) return <p role="status">正在加载运行态…</p>;
  if (runtime.isError) return (
    <p role="alert">
      {runtime.error instanceof RuntimeProjectMismatchError ? "运行态项目校验失败" : "运行态加载失败"}
    </p>
  );
  return <RuntimePage snapshot={runtime.data} />;
}
