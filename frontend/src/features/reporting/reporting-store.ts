import { createStore } from "zustand/vanilla";

import type { RuntimeEvent } from "../../api/event-stream";
import type { components } from "../../api/generated/schema";

export type ReportingSnapshotResponse = components["schemas"]["ReportingSnapshotResponse"];

export type ReportingStatus =
  | "planning"
  | "running"
  | "waiting_user"
  | "revising"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "unknown";

export interface ReportingRunView {
  readonly active: boolean;
  readonly canCancel: boolean;
  readonly canResume: boolean;
  readonly error: string;
  readonly id: string;
  readonly operation: string;
  readonly rawStatus: string;
  readonly status: ReportingStatus;
  readonly taskId: string;
  readonly title: string;
}

export interface ReportingOutputView {
  readonly exists: boolean;
  readonly path: string;
  readonly sha256: string | null;
  readonly size: number;
}

export interface ReportingWaitingInputView {
  readonly affectedModules: readonly string[];
  readonly allowedActions: readonly string[];
  readonly decisionId: string;
  readonly missingItems: readonly string[];
  readonly selectedAction: string;
  readonly status: string;
}

export interface ReportingAgentView {
  readonly id: string;
  readonly resultPath: string;
  readonly status: string;
  readonly taskId: string;
}

export interface ReportingTimelineItem {
  readonly agentId: string;
  readonly id: string;
  readonly kind: string;
  readonly summary: string;
  readonly taskId: string;
  readonly timestamp: string;
}

export interface ReportingVerificationView {
  readonly message: string;
  readonly status: "failed" | "passed" | "pending";
}

export interface ReportingSnapshotView {
  readonly agents: Readonly<Record<string, ReportingAgentView>>;
  readonly checkpoint: Readonly<Record<string, unknown>>;
  readonly evidence: Readonly<Record<string, unknown>>;
  readonly outputs: readonly ReportingOutputView[];
  readonly revision: Readonly<Record<string, unknown>>;
  readonly run: ReportingRunView;
  readonly state: Readonly<Record<string, unknown>>;
  readonly timeline: readonly ReportingTimelineItem[];
  readonly verification: ReportingVerificationView;
  readonly waitingInput: readonly ReportingWaitingInputView[];
}

export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function asText(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function normalizedStatus(rawStatus: string, activity = ""): ReportingStatus {
  const status = rawStatus.trim().toLowerCase();
  const currentActivity = activity.trim().toLowerCase();
  if (status === "completed") return "completed";
  if (["failed", "blocked", "stopped_incomplete"].includes(status)) return "failed";
  if (status === "cancelled") return "cancelled";
  if (status === "interrupted") return "interrupted";
  if (["needs_user_decision", "needs_decision", "waiting_user"].includes(status)) {
    return "waiting_user";
  }
  if (status.includes("revision") || currentActivity.includes("revision")) return "revising";
  if (["in_progress", "running", "accepted"].includes(status)) return "running";
  if (["planning", "pending", "queued"].includes(status)) return "planning";
  if (currentActivity.includes("revision")) return "revising";
  return status ? "running" : "unknown";
}

const resumableStatuses = new Set([
  "blocked",
  "cancelled",
  "failed",
  "in_progress",
  "needs_decision",
]);

export function normalizeRun(
  value: unknown,
  stateValue: unknown = {},
): ReportingRunView {
  const run = asRecord(value);
  const state = asRecord(stateValue);
  const id = asText(run.run_id) || asText(run.runId) || asText(state.run_id) || "unknown-run";
  const rawStatus = asText(run.status) || asText(state.status);
  const activity = asText(state.activity);
  const active = run.active === true;
  const operation = asText(run.operation) || asText(state.operation) || "full_report";
  return {
    active,
    canCancel: active,
    canResume: !active && resumableStatuses.has(rawStatus),
    error: asText(run.error) || asText(state.error),
    id,
    operation,
    rawStatus,
    status: normalizedStatus(rawStatus, activity),
    taskId: asText(run.task_id) || asText(run.taskId),
    title: asText(run.title) || asText(run.instruction) || id,
  };
}

function normalizeOutput(value: unknown): ReportingOutputView {
  const output = asRecord(value);
  return {
    exists: output.exists === true,
    path: asText(output.path),
    sha256: asText(output.sha256) || null,
    size: asNumber(output.size),
  };
}

function normalizeWaitingInput(value: unknown): ReportingWaitingInputView {
  const input = asRecord(value);
  return {
    affectedModules: asStringArray(input.affected_modules ?? input.affectedModules),
    allowedActions: asStringArray(input.allowed_actions ?? input.allowedActions),
    decisionId: asText(input.decision_id) || asText(input.decisionId),
    missingItems: asStringArray(input.missing_items ?? input.missingItems),
    selectedAction: asText(input.selected_action) || asText(input.selectedAction),
    status: asText(input.status, "pending"),
  };
}

function snapshotAgents(state: Record<string, unknown>): Record<string, ReportingAgentView> {
  const agents: Record<string, ReportingAgentView> = {};
  const completed = new Set(asStringArray(state.completed_modules));
  for (const moduleId of asStringArray(state.specialist_modules)) {
    const id = `module-${moduleId}-specialist`;
    agents[id] = {
      id,
      resultPath: "",
      status: completed.has(moduleId) ? "completed" : "running",
      taskId: `module-${moduleId}`,
    };
  }
  return agents;
}

function verifiedOutputState(
  run: ReportingRunView,
  outputs: readonly ReportingOutputView[],
): ReportingVerificationView {
  if (run.status === "completed") {
    const valid = outputs.length > 0
      && outputs.every((output) => output.exists && output.sha256 !== null && output.path.length > 0);
    return valid
      ? { message: "服务端已验证交付文件", status: "passed" }
      : { message: "输出文件缺失或未通过服务端校验", status: "failed" };
  }
  if (run.status === "failed") {
    return { message: run.error || "报告生成或输出校验失败", status: "failed" };
  }
  return { message: "等待服务端完成输出校验", status: "pending" };
}

export function normalizeReportingSnapshot(
  value: ReportingSnapshotResponse,
): ReportingSnapshotView {
  const state = asRecord(value.state);
  const checkpoint = asRecord(value.checkpoint);
  const outputs = value.outputs.map(normalizeOutput);
  let run = normalizeRun(value.run, state);
  const hasCheckpoint = Boolean(asText(checkpoint.run_id) || asText(checkpoint.status));
  if (run.canResume && !hasCheckpoint) {
    run = { ...run, canResume: false };
  }
  const verification = verifiedOutputState(run, outputs);
  if (run.status === "completed" && verification.status === "failed") {
    run = { ...run, canResume: false, status: "failed" };
  }
  return {
    agents: snapshotAgents(state),
    checkpoint,
    evidence: asRecord(value.evidence),
    outputs,
    revision: asRecord(value.revision),
    run,
    state,
    timeline: [],
    verification,
    waitingInput: value.waitingInput.map(normalizeWaitingInput),
  };
}

function eventRunId(event: RuntimeEvent, fallback: string | null): string | null {
  const direct = asText(event.payload.run_id) || asText(event.payload.runId);
  if (direct) return direct;
  const workflowId = asText(event.payload.workflow_id) || asText(event.payload.workflowId);
  if (workflowId.includes(":")) return workflowId.slice(workflowId.lastIndexOf(":") + 1);
  return fallback;
}

function eventAgentId(event: RuntimeEvent): string {
  return asText(event.payload.sender)
    || asText(event.payload.agent_type)
    || asText(event.payload.agentId)
    || event.agentId
    || "reporting";
}

function eventSummary(event: RuntimeEvent, agentId: string): string {
  if (event.type === "reporting.agent_result.changed") {
    return `${agentId} · ${asText(event.payload.status, "updated")}`;
  }
  return asText(event.payload.summary)
    || asText(event.payload.content)
    || asText(event.payload.reason)
    || asText(event.payload.note_kind)
    || event.type;
}

function applyReportingEvent(
  snapshot: ReportingSnapshotView,
  event: RuntimeEvent,
): ReportingSnapshotView {
  const agentId = eventAgentId(event);
  const taskId = asText(event.payload.task_id) || asText(event.payload.taskId);
  const timeline: ReportingTimelineItem = {
    agentId,
    id: event.eventId,
    kind: event.type,
    summary: eventSummary(event, agentId),
    taskId,
    timestamp: event.timestamp,
  };
  const agentStatus = asText(event.payload.status)
    || (event.type === "reporting.blocked" ? "blocked" : "running");
  const agents = {
    ...snapshot.agents,
    [agentId]: {
      id: agentId,
      resultPath: asText(event.payload.result_path),
      status: agentStatus,
      taskId,
    },
  };
  let run = snapshot.run;
  const checkpoint = event.type === "checkpoint.created"
    ? { ...snapshot.checkpoint, ...event.payload }
    : snapshot.checkpoint;
  if (event.type === "report.status.changed") {
    const rawStatus = asText(event.payload.status) || run.rawStatus;
    run = { ...run, rawStatus, status: normalizedStatus(rawStatus, asText(snapshot.state.activity)) };
  } else if (event.type === "reporting.blocked") {
    run = { ...run, rawStatus: "blocked", status: "failed" };
  }
  return { ...snapshot, agents, checkpoint, run, timeline: [...snapshot.timeline, timeline] };
}

function placeholderSnapshot(runId: string): ReportingSnapshotView {
  return normalizeReportingSnapshot({
    checkpoint: {},
    evidence: {},
    outputs: [],
    revision: {},
    run: { run_id: runId },
    state: {},
    waitingInput: [],
  });
}

function isReportingEvent(event: RuntimeEvent): boolean {
  if (
    event.type.startsWith("report.")
    || event.type.startsWith("reporting.")
    || event.type === "checkpoint.created"
  ) {
    return true;
  }
  if (event.type !== "agent.status.changed") return false;
  return /analyst|auditor|chief|editor|report|research|reviewer|specialist/i.test(eventAgentId(event));
}

function continuous(lastSequence: number | null, event: RuntimeEvent): boolean {
  if (lastSequence === null || event.sequence === lastSequence + 1) return true;
  const delivery = asRecord(event.payload.delivery);
  return asNumber(delivery.fromSequence) === lastSequence + 1
    && asNumber(delivery.toSequence) === event.sequence;
}

export interface ReportingStoreState {
  readonly appliedEventIds: ReadonlySet<string>;
  readonly bufferedEvents: Readonly<Record<string, readonly RuntimeEvent[]>>;
  readonly lastSequence: number | null;
  readonly pendingSnapshots: ReadonlySet<string>;
  readonly refreshRequested: boolean;
  readonly runs: readonly ReportingRunView[];
  readonly selectedRunId: string | null;
  readonly snapshots: Readonly<Record<string, ReportingSnapshotView>>;
  readonly stale: boolean;
  readonly streamId: string | null;
  readonly applyEvent: (event: RuntimeEvent) => void;
  readonly beginSnapshot: (runId: string) => void;
  readonly failSnapshot: (runId: string) => void;
  readonly hydrateRuns: (runs: readonly Record<string, unknown>[]) => void;
  readonly hydrateSnapshot: (runId: string, snapshot: ReportingSnapshotResponse) => void;
  readonly resetStream: (streamId: string) => void;
  readonly selectRun: (runId: string | null) => void;
}

export function createReportingStore(initialStreamId: string | null = null) {
  return createStore<ReportingStoreState>()((set) => ({
    appliedEventIds: new Set(),
    applyEvent: (event) => set((current) => {
      if (current.stale || current.appliedEventIds.has(event.eventId)) return current;
      if (current.streamId !== null && current.streamId !== event.streamId) {
        return { ...current, refreshRequested: true, stale: true };
      }
      if (current.lastSequence !== null && event.sequence <= current.lastSequence) return current;
      if (!continuous(current.lastSequence, event)) {
        return { ...current, refreshRequested: true, stale: true };
      }
      const base = {
        ...current,
        appliedEventIds: new Set([...current.appliedEventIds, event.eventId]),
        lastSequence: event.sequence,
      };
      if (!isReportingEvent(event)) return base;
      const runId = eventRunId(event, current.selectedRunId);
      if (!runId) return base;
      if (current.pendingSnapshots.has(runId)) {
        return {
          ...base,
          bufferedEvents: {
            ...current.bufferedEvents,
            [runId]: [...(current.bufferedEvents[runId] ?? []), event],
          },
        };
      }
      const existing = current.snapshots[runId] ?? placeholderSnapshot(runId);
      const snapshot = applyReportingEvent(existing, event);
      return {
        ...base,
        runs: current.runs.some((run) => run.id === runId)
          ? current.runs.map((run) => run.id === runId ? snapshot.run : run)
          : [snapshot.run, ...current.runs],
        snapshots: { ...current.snapshots, [runId]: snapshot },
      };
    }),
    beginSnapshot: (runId) => set((current) => ({
      ...current,
      bufferedEvents: {
        ...current.bufferedEvents,
        [runId]: current.bufferedEvents[runId] ?? [],
      },
      pendingSnapshots: new Set([...current.pendingSnapshots, runId]),
    })),
    bufferedEvents: {},
    failSnapshot: (runId) => set((current) => {
      const pendingSnapshots = new Set(current.pendingSnapshots);
      pendingSnapshots.delete(runId);
      return { ...current, pendingSnapshots };
    }),
    hydrateRuns: (values) => set((current) => ({
      ...current,
      runs: values.map((value) => normalizeRun(value)),
    })),
    hydrateSnapshot: (runId, value) => set((current) => {
      let snapshot = normalizeReportingSnapshot(value);
      for (const event of current.bufferedEvents[runId] ?? []) {
        snapshot = applyReportingEvent(snapshot, event);
      }
      const bufferedEvents = { ...current.bufferedEvents };
      delete bufferedEvents[runId];
      const pendingSnapshots = new Set(current.pendingSnapshots);
      pendingSnapshots.delete(runId);
      return {
        ...current,
        bufferedEvents,
        pendingSnapshots,
        runs: current.runs.some((run) => run.id === runId)
          ? current.runs.map((run) => run.id === runId ? snapshot.run : run)
          : [snapshot.run, ...current.runs],
        snapshots: { ...current.snapshots, [runId]: snapshot },
      };
    }),
    lastSequence: null,
    pendingSnapshots: new Set(),
    refreshRequested: false,
    resetStream: (streamId) => set((current) => ({
      ...current,
      appliedEventIds: new Set(),
      bufferedEvents: {},
      lastSequence: null,
      pendingSnapshots: new Set(),
      refreshRequested: false,
      stale: false,
      streamId,
    })),
    runs: [],
    selectRun: (runId) => set((current) => ({ ...current, selectedRunId: runId })),
    selectedRunId: null,
    snapshots: {},
    stale: false,
    streamId: initialStreamId,
  }));
}

export type ReportingStore = ReturnType<typeof createReportingStore>;
