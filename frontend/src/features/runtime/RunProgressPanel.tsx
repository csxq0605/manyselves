import type { ReactNode } from "react";

import { shortAgentLabel } from "../agents/agent-label";
import {
  reportingItemNoun,
  reportingStepText,
  reportingWorkflowLabel,
} from "../reporting/reporting-run-presentation";
import type {
  RunProgressDetail,
  RunProgressEvent,
  RunProgressNode,
  RunProgressResponse,
  RunProgressStep,
} from "../runs/workflow-api";

const statusText: Readonly<Record<string, string>> = {
  completed: "完成",
  failed: "失败",
  pending: "未开始",
  running: "进行中",
  skipped: "跳过",
  waiting: "等待输入",
};

function stepStatusText(status: string): string {
  return statusText[status] ?? status;
}

function timeOf(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? ""
    : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function StepList({ node }: { readonly node: RunProgressNode }) {
  return (
    <ol className="run-progress__steps">
      {node.steps.map((step) => (
        <li
          className={`run-progress__step run-progress__step--${step.status}`}
          key={step.id}
          title={step.id}
        >
          <span aria-hidden="true" className="run-progress__dot" />
          <span>{reportingStepText(step.id).label}</span>
          <small>{stepStatusText(step.status)}</small>
        </li>
      ))}
    </ol>
  );
}

function currentStep(node: RunProgressNode): RunProgressStep | undefined {
  return node.steps.find((step) => step.status === "running" || step.status === "waiting")
    ?? node.steps.find((step) => step.status === "failed")
    ?? node.steps.find((step) => step.id === node.current);
}

function StageNote({ step }: { readonly step: RunProgressStep | undefined }) {
  if (!step) return null;
  const text = reportingStepText(step.id);
  return (
    <p className="run-progress__note">
      <strong>{text.label}</strong>
      {text.note ? <span>{text.note}</span> : null}
    </p>
  );
}

/** One running item of a map, on one line: its key and the step it is on. */
function itemPosition(node: RunProgressNode | null): string {
  if (!node) return "";
  const parts: string[] = [];
  let current: RunProgressNode | null = node;
  for (let depth = 0; current && depth < 4; depth += 1) {
    const step = currentStep(current);
    if (step) parts.push(reportingStepText(step.id).label);
    const detail: RunProgressDetail | null = current.detail;
    if (detail?.kind === "for_each") {
      parts.push(`第 ${Math.min(detail.index + 1, detail.total)} / ${detail.total} 项`);
      break;
    }
    if (detail?.kind === "map") {
      parts.push(`${detail.completed} / ${detail.total} ${reportingItemNoun(detail.workflow)}`);
      break;
    }
    current = detail?.kind === "subworkflow" ? detail.child : null;
  }
  return parts.join(" · ");
}

function MapDetail({ detail }: { readonly detail: Extract<RunProgressDetail, { kind: "map" }> }) {
  const noun = reportingItemNoun(detail.workflow);
  const percent = detail.total > 0 ? Math.round((detail.completed / detail.total) * 100) : 0;
  return (
    <div className="run-progress__map">
      <div className="run-progress__bar" aria-label={`已完成 ${detail.completed} / ${detail.total}`}>
        <span style={{ width: `${percent}%` }} />
      </div>
      <p className="run-progress__counts">
        {detail.total} 个{noun}：完成 {detail.completed} · 进行中 {detail.running}
        {detail.queued > 0 ? ` · 排队 ${detail.queued}` : ""}
        {detail.failed > 0 ? ` · 失败 ${detail.failed}` : ""}
      </p>
      {detail.items.length > 0 ? (
        <ul className="run-progress__items">
          {detail.items.map((item) => (
            <li className={`run-progress__item run-progress__item--${item.status}`} key={item.key}>
              <code>{item.key}</code>
              <span>{itemPosition(item.node) || stepStatusText(item.status)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {detail.queued_keys.length > 0 ? (
        <p className="run-progress__queued">排队：{detail.queued_keys.join("、")}</p>
      ) : null}
    </div>
  );
}

function Detail({ detail }: { readonly detail: RunProgressDetail | null }): ReactNode {
  if (!detail) return null;
  if (detail.kind === "subworkflow") {
    return detail.child ? <NodeView node={detail.child} /> : null;
  }
  if (detail.kind === "map") return <MapDetail detail={detail} />;
  if (detail.kind === "for_each") {
    return (
      <p className="run-progress__counts">
        {reportingStepText(detail.action_id).label}：第 {Math.min(detail.index + 1, detail.total)} / {detail.total} 项
      </p>
    );
  }
  return (
    <div className="run-progress__branches">
      {detail.branches.map((branch) => (
        <section className="run-progress__branch" key={branch.branch_id}>
          <h4>
            {reportingStepText(branch.start).label}
            <small>{stepStatusText(branch.status)}</small>
          </h4>
          {branch.node ? <NodeView node={branch.node} /> : null}
        </section>
      ))}
    </div>
  );
}

function NodeView({ node }: { readonly node: RunProgressNode }) {
  const step = currentStep(node);
  const finished = node.status === "completed";
  return (
    <div className="run-progress__node">
      <StepList node={node} />
      {finished ? null : <StageNote step={step} />}
      {finished ? null : <Detail detail={node.detail} />}
    </div>
  );
}

function eventText(event: RunProgressEvent): string {
  const step = event.action_id ? reportingStepText(event.action_id).label : "";
  switch (event.kind) {
    case "agent.invoked":
      return `${step}：调用 ${event.agent_id ? shortAgentLabel(event.agent_id) : "Agent"}`;
    case "tool.invoked":
      return `${step}：程序步骤 ${event.tool_id ?? ""}${event.reused ? "（复用上次结果）" : ""}`;
    case "map.item.completed":
      return `${step}：${event.key ?? ""} 完成`;
    case "map.item.failed":
      return `${step}：${event.key ?? ""} 失败`;
    case "action.failed":
      return `${step} 失败`;
    case "action.waiting":
      return `${step} 等待输入`;
    case "workflow.started":
      return `开始：${reportingWorkflowLabel(event.workflow_id)}`;
    case "workflow.completed":
      return `完成：${reportingWorkflowLabel(event.workflow_id)}`;
    case "workflow.failed":
      return `失败：${reportingWorkflowLabel(event.workflow_id)}`;
    case "workflow.waiting":
      return `等待输入：${reportingWorkflowLabel(event.workflow_id)}`;
    case "output.published":
      return `发布结果：${step}`;
    default:
      return event.kind;
  }
}

export function RunProgressEvents({ events }: { readonly events: readonly RunProgressEvent[] }) {
  if (events.length === 0) return <p className="operations-empty">还没有运行动态</p>;
  return (
    <ol className="run-progress__events">
      {events.map((event, index) => (
        <li
          className={event.error || event.kind.endsWith("failed") ? "run-progress__event--failed" : undefined}
          key={`${event.ts ?? ""}-${event.kind}-${index}`}
        >
          <time>{timeOf(event.ts)}</time>
          <span>{eventText(event)}</span>
          {event.error ? <small>{event.error}</small> : null}
        </li>
      ))}
    </ol>
  );
}

export function RunProgressPanel({ progress }: { readonly progress: RunProgressResponse }) {
  if (!progress.root) return <p className="operations-empty">还没有运行进度</p>;
  return (
    <div className="run-progress">
      <NodeView node={progress.root} />
    </div>
  );
}
