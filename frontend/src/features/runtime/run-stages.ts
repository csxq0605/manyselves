import {
  reportingItemNoun,
  reportingStepText,
  type ReportingStage,
} from "../reporting/reporting-run-presentation";
import type { RunProgressDetail, RunProgressNode, RunProgressResponse } from "../runs/workflow-api";

export type StageStatus = "completed" | "failed" | "pending" | "running" | "waiting";

export interface StageView {
  readonly label: string;
  /** Milliseconds spent, or null when the trace has no times for it. */
  readonly elapsedMs: number | null;
  readonly status: StageStatus;
}

/** Every visible step's status, keyed `workflow/action`, across the whole state tree. */
function collectStatuses(root: RunProgressNode): {
  readonly byStep: Map<string, string>;
  readonly byWorkflow: Map<string, string>;
} {
  const byStep = new Map<string, string>();
  const byWorkflow = new Map<string, string>();
  const visit = (node: RunProgressNode | null) => {
    if (!node) return;
    for (const step of node.steps) {
      const key = `${node.workflow_id}/${step.id}`;
      const known = byStep.get(key);
      // Parallel branches repeat the parent's steps; the branch knows better.
      if (!known || known === "pending") byStep.set(key, step.status);
      if (step.workflow) byWorkflow.set(step.workflow, step.status);
    }
    visitDetail(node.detail);
  };
  const visitDetail = (detail: RunProgressDetail | null) => {
    if (!detail) return;
    if (detail.kind === "subworkflow") visit(detail.child);
    if (detail.kind === "parallel") detail.branches.forEach((branch) => visit(branch.node));
  };
  visit(root);
  return { byStep, byWorkflow };
}

function stageStatus(statuses: readonly string[]): StageStatus {
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("waiting")) return "waiting";
  if (statuses.includes("running")) return "running";
  const done = statuses.filter((status) => status === "completed" || status === "skipped").length;
  if (done === statuses.length) return "completed";
  return done > 0 ? "running" : "pending";
}

function elapsed(
  keys: readonly string[],
  spans: RunProgressResponse["spans"],
  status: StageStatus,
  now: number,
): number | null {
  if (!spans) return null;
  let start: number | null = null;
  let end: number | null = null;
  for (const key of keys) {
    const span = spans[key];
    if (span?.start) {
      const value = Date.parse(span.start);
      if (!Number.isNaN(value)) start = start === null ? value : Math.min(start, value);
    }
    if (span?.end) {
      const value = Date.parse(span.end);
      if (!Number.isNaN(value)) end = end === null ? value : Math.max(end, value);
    }
  }
  if (start === null) return null;
  if (status === "running" || status === "waiting") return Math.max(0, now - start);
  return end === null ? null : Math.max(0, end - start);
}

/** The Run as a short list of stages a person can follow. */
export function runStages(
  progress: RunProgressResponse,
  stages: readonly ReportingStage[],
  now = Date.now(),
): StageView[] {
  if (!progress.root) return stages.map((stage) => ({ elapsedMs: null, label: stage.label, status: "pending" }));
  const { byStep, byWorkflow } = collectStatuses(progress.root);
  return stages.map((stage) => {
    // Steps the trace shows decide; a step it does not show (not a visible kind,
    // or its workflow not open now) only counts when none of the stage is shown.
    const known = stage.steps.flatMap((key) => {
      const status = byStep.get(key);
      return status ? [status] : [];
    });
    const statuses = known.length > 0 ? known : stage.steps.map((key) => {
      const caller = byWorkflow.get(key.slice(0, key.indexOf("/")));
      return caller === "completed" || caller === "skipped" ? "completed" : "pending";
    });
    return { stage, status: stageStatus(statuses) };
  }).map(({ stage, status }, index, all) => {
    // Stages run in order: one not started while a later one has is passed.
    const passed = status === "pending" && all.slice(index + 1).some((later) => later.status !== "pending");
    const shown: StageStatus = passed ? "completed" : status;
    return { elapsedMs: elapsed(stage.steps, progress.spans, shown, now), label: stage.label, status: shown };
  });
}

/** Total time from the first step that started, or null without times. */
export function runElapsed(progress: RunProgressResponse, running: boolean, now = Date.now()): number | null {
  const spans = Object.values(progress.spans ?? {});
  const starts = spans.flatMap((span) => (span.start ? [Date.parse(span.start)] : [])).filter((n) => !Number.isNaN(n));
  if (starts.length === 0) return null;
  const ends = spans.flatMap((span) => (span.end ? [Date.parse(span.end)] : [])).filter((n) => !Number.isNaN(n));
  const end = running ? now : Math.max(...ends, ...starts);
  return Math.max(0, end - Math.min(...starts));
}

function deepestStep(node: RunProgressNode): string | null {
  let current: RunProgressNode | null = node;
  let found: string | null = null;
  for (let depth = 0; current && depth < 8; depth += 1) {
    const step = current.steps.find((item) => item.status === "running" || item.status === "waiting");
    if (step) found = step.id;
    const detail: RunProgressDetail | null = current.detail;
    if (detail?.kind === "subworkflow") current = detail.child;
    else if (detail?.kind === "parallel") current = detail.branches.find((b) => b.status === "running")?.node ?? null;
    else current = null;
  }
  return found;
}

/** One line on what the running stage is doing: how far each batch of parallel work is. */
export function currentLine(progress: RunProgressResponse): string | null {
  if (!progress.root) return null;
  const parts: string[] = [];
  const visit = (node: RunProgressNode | null) => {
    if (!node || node.status === "completed") return;
    const detail = node.detail;
    if (!detail) return;
    if (detail.kind === "map") {
      if (detail.completed < detail.total) {
        const noun = reportingItemNoun(detail.workflow);
        parts.push(`${reportingStepText(detail.action_id).label}：${detail.total} 个${noun}中 ${detail.completed} 个完成`);
      }
      return;
    }
    if (detail.kind === "subworkflow") visit(detail.child);
    if (detail.kind === "parallel") detail.branches.forEach((branch) => visit(branch.node));
  };
  visit(progress.root);
  if (parts.length > 0) return parts.join(" · ");
  const step = deepestStep(progress.root);
  if (!step) return null;
  const text = reportingStepText(step);
  return text.note ? `${text.label}：${text.note}` : text.label;
}

export function formatElapsed(ms: number | null): string {
  if (ms === null) return "—";
  const seconds = Math.round(ms / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = String(seconds % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}
