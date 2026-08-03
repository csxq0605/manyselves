import type { RuntimeEvent } from "../../api/event-stream";
import type { components } from "../../api/generated/schema";

type RuntimeSnapshot = components["schemas"]["RuntimeSnapshotResponse"];

export type AgentActivityStatus = "error" | "idle" | "running" | "thinking" | "waiting" | string;

export interface RuntimeAgentView {
  readonly id: string;
  readonly label: string;
  readonly status: AgentActivityStatus;
}

export interface RuntimeMessageView {
  readonly agentId: string;
  readonly content: string;
  readonly id: string;
  readonly sessionId: string | null;
  readonly status: "streaming" | "completed" | "interrupted";
  readonly thinking: string;
  readonly timestamp: string;
}

export interface RuntimeQueueView {
  readonly agentId: string;
  readonly items: readonly string[];
  readonly pendingCount: number;
}

export interface RuntimeTaskView {
  readonly blocking: boolean;
  readonly brief: string;
  readonly id: string;
  readonly sourceAgent: string;
  readonly status: string;
  readonly targetAgent: string;
}

export interface RuntimeToolView {
  readonly agentId: string;
  readonly arguments: unknown;
  readonly error: unknown;
  readonly id: string;
  readonly name: string;
  readonly result: unknown;
  readonly status: "running" | "completed" | "failed";
}

export interface RuntimeDebugView {
  readonly agentId: string;
  readonly durationMs: number;
  readonly error: unknown;
  readonly id: string;
  readonly model: string;
  readonly status: string;
  readonly timestamp: string;
  readonly tokensIn: number;
  readonly tokensOut: number;
}

export interface RuntimeCheckpointView {
  readonly agentId: string;
  readonly description: string;
  readonly epoch: number;
  readonly id: string;
  readonly messageId: string | null;
  readonly source: string;
  readonly timestamp: string;
}

export interface RuntimeNoticeView {
  readonly agentId: string;
  readonly content: string;
  readonly id: string;
  readonly kind: "notice" | "interrupt" | "error";
  readonly timestamp: string;
}

export interface RuntimeInterruptView {
  readonly active: boolean;
  readonly agentId: string;
  readonly messageId: string | null;
  readonly timestamp: string;
}

export interface RuntimeUsageView {
  readonly durationMs: number;
  readonly tokensIn: number;
  readonly tokensOut: number;
}

export interface AgentEventState {
  readonly agents: Readonly<Record<string, RuntimeAgentView>>;
  readonly appliedEventIds: ReadonlySet<string>;
  readonly checkpoints: readonly RuntimeCheckpointView[];
  readonly debug: readonly RuntimeDebugView[];
  readonly interrupt: RuntimeInterruptView | null;
  readonly lastSequence: number | null;
  readonly messages: Readonly<Record<string, RuntimeMessageView>>;
  readonly notices: readonly RuntimeNoticeView[];
  readonly queues: Readonly<Record<string, RuntimeQueueView>>;
  readonly refreshRequested: boolean;
  readonly runtimeError: RuntimeNoticeView | null;
  readonly stale: boolean;
  readonly streamId: string | null;
  readonly tasks: Readonly<Record<string, RuntimeTaskView>>;
  readonly tools: Readonly<Record<string, RuntimeToolView>>;
  readonly usage: RuntimeUsageView;
}

const secretKeyPattern = /authorization|api[_-]?key|access[_-]?token|password|secret/i;
const secretValuePatterns = [
  /\bBearer\s+[^\s,;]+/gi,
  /\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+/gi,
] as const;

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function normalizeAgentId(value: unknown): string {
  const normalized = text(value, "main").trim().toLowerCase();
  return normalized || "main";
}

function labelFor(value: unknown, id: string): string {
  const label = text(value).trim();
  return label || id;
}

export function maskSensitive(value: unknown, key = ""): unknown {
  if (secretKeyPattern.test(key)) {
    return "[REDACTED]";
  }
  if (typeof value === "string") {
    return secretValuePatterns.reduce(
      (masked, pattern) => masked.replace(pattern, (match, name: string | undefined) => (
        name ? `${name}=[REDACTED]` : "Bearer [REDACTED]"
      )),
      value,
    );
  }
  if (Array.isArray(value)) {
    return value.map((item) => maskSensitive(item));
  }
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value).map(([entryKey, entryValue]) => [
        entryKey,
        maskSensitive(entryValue, entryKey),
      ]),
    );
  }
  return value;
}

export function emptyAgentState(streamId: string | null = null): AgentEventState {
  return {
    agents: {},
    appliedEventIds: new Set(),
    checkpoints: [],
    debug: [],
    interrupt: null,
    lastSequence: null,
    messages: {},
    notices: [],
    queues: {},
    refreshRequested: false,
    runtimeError: null,
    stale: false,
    streamId,
    tasks: {},
    tools: {},
    usage: { durationMs: 0, tokensIn: 0, tokensOut: 0 },
  };
}

function withAgent(
  agents: Readonly<Record<string, RuntimeAgentView>>,
  sourceId: unknown,
  status?: string,
): Readonly<Record<string, RuntimeAgentView>> {
  const id = normalizeAgentId(sourceId);
  const current = agents[id];
  return {
    ...agents,
    [id]: {
      id,
      label: current?.label ?? labelFor(sourceId, id),
      status: status ?? current?.status ?? "idle",
    },
  };
}

function eventAgentSource(event: RuntimeEvent): unknown {
  return event.agentId
    ?? event.payload.agentId
    ?? event.payload.agent_id
    ?? event.payload.agent_type
    ?? "main";
}

function hasContinuousSequence(state: AgentEventState, event: RuntimeEvent): boolean {
  if (state.lastSequence === null || event.sequence === state.lastSequence + 1) {
    return true;
  }
  const delivery = record(event.payload.delivery);
  return number(delivery.fromSequence) === state.lastSequence + 1
    && number(delivery.toSequence) === event.sequence;
}

function applied(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  return {
    ...state,
    appliedEventIds: new Set([...state.appliedEventIds, event.eventId]),
    lastSequence: event.sequence,
  };
}

function eventToolId(event: RuntimeEvent): string {
  return text(event.payload.toolCallId) || text(event.payload.tool_call_id) || event.eventId;
}

function messageEvent(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  const id = event.messageId ?? (text(event.payload.message_id) || event.eventId);
  const agentSource = eventAgentSource(event);
  const agentId = normalizeAgentId(agentSource);
  const current = state.messages[id];
  const completed = event.type === "agent.message.completed";
  const content = text(event.payload.content);
  const thinking = text(event.payload.thinking);
  return {
    ...state,
    agents: withAgent(state.agents, agentSource, completed ? "idle" : "running"),
    messages: {
      ...state.messages,
      [id]: {
        agentId,
        content: completed ? (content || current?.content || "") : `${current?.content ?? ""}${content}`,
        id,
        sessionId: event.sessionId ?? null,
        status: completed ? "completed" : "streaming",
        thinking: completed ? (thinking || current?.thinking || "") : `${current?.thinking ?? ""}${thinking}`,
        timestamp: event.timestamp,
      },
    },
  };
}

function toolEvent(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  const id = eventToolId(event);
  const agentSource = eventAgentSource(event);
  const agentId = normalizeAgentId(agentSource);
  const current = state.tools[id];
  const status = event.type === "tool.started"
    ? "running"
    : event.type === "tool.failed" ? "failed" : "completed";
  return {
    ...state,
    agents: withAgent(state.agents, agentSource, status === "running" ? "running" : undefined),
    tools: {
      ...state.tools,
      [id]: {
        agentId,
        arguments: event.type === "tool.started"
          ? maskSensitive(event.payload.arguments)
          : current?.arguments ?? null,
        error: event.type === "tool.failed" ? maskSensitive(event.payload.error) : null,
        id,
        name: text(event.payload.tool_name) || text(event.payload.name) || current?.name || "tool",
        result: event.type === "tool.completed" ? maskSensitive(event.payload.result) : null,
        status,
      },
    },
  };
}

function statusEvent(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  const source = eventAgentSource(event);
  return {
    ...state,
    agents: withAgent(state.agents, source, text(event.payload.status, "idle")),
  };
}

function queueEvent(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  const source = eventAgentSource(event);
  const agentId = normalizeAgentId(source);
  const items = stringArray(event.payload.queued_messages ?? event.payload.items);
  return {
    ...state,
    agents: withAgent(state.agents, source),
    queues: {
      ...state.queues,
      [agentId]: { agentId, items, pendingCount: items.length },
    },
  };
}

function taskEvent(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  const id = text(event.payload.task_id) || text(event.payload.taskId) || event.eventId;
  const source = event.payload.source_agent ?? event.payload.sourceAgent ?? "main";
  const target = event.payload.target_agent ?? event.payload.targetAgent ?? "main";
  const sourceAgent = normalizeAgentId(source);
  const targetAgent = normalizeAgentId(target);
  const current = state.tasks[id];
  return {
    ...state,
    agents: withAgent(withAgent(state.agents, source), target),
    tasks: {
      ...state.tasks,
      [id]: {
        blocking: event.payload.blocking === true || current?.blocking === true,
        brief: text(event.payload.brief) || current?.brief || "",
        id,
        sourceAgent,
        status: text(event.payload.action) || text(event.payload.status) || current?.status || "pending",
        targetAgent,
      },
    },
  };
}

function debugEvent(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  const agentSource = eventAgentSource(event);
  const agentId = normalizeAgentId(agentSource);
  const entry: RuntimeDebugView = {
    agentId,
    durationMs: number(event.payload.duration_ms),
    error: maskSensitive(event.payload.error),
    id: event.eventId,
    model: text(maskSensitive(event.payload.model)),
    status: text(event.payload.status, event.type.endsWith("failed") ? "error" : "success"),
    timestamp: event.timestamp,
    tokensIn: number(event.payload.tokens_in),
    tokensOut: number(event.payload.tokens_out),
  };
  return {
    ...state,
    agents: withAgent(state.agents, agentSource, event.type.endsWith("failed") ? "error" : undefined),
    debug: [...state.debug, entry],
    usage: {
      durationMs: state.usage.durationMs + entry.durationMs,
      tokensIn: state.usage.tokensIn + entry.tokensIn,
      tokensOut: state.usage.tokensOut + entry.tokensOut,
    },
  };
}

function noticeEvent(
  state: AgentEventState,
  event: RuntimeEvent,
  kind: RuntimeNoticeView["kind"],
): AgentEventState {
  const source = eventAgentSource(event);
  const agentId = normalizeAgentId(source);
  const content = text(event.payload.content) || text(event.payload.message) || event.type;
  const notice: RuntimeNoticeView = {
    agentId,
    content,
    id: event.eventId,
    kind,
    timestamp: event.timestamp,
  };
  const waiting = kind === "notice" && /wait/i.test(content);
  return {
    ...state,
    agents: withAgent(
      state.agents,
      source,
      kind === "error" ? "error" : waiting ? "waiting" : undefined,
    ),
    interrupt: kind === "interrupt" ? {
      active: true,
      agentId,
      messageId: event.messageId ?? null,
      timestamp: event.timestamp,
    } : state.interrupt,
    notices: [...state.notices, notice],
    runtimeError: kind === "error" ? notice : state.runtimeError,
  };
}

function checkpointEvent(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  const source = eventAgentSource(event);
  const checkpoint: RuntimeCheckpointView = {
    agentId: normalizeAgentId(source),
    description: text(event.payload.description),
    epoch: number(event.payload.epoch),
    id: text(event.payload.checkpoint_id) || text(event.payload.checkpointId) || event.eventId,
    messageId: text(event.payload.message_id) || event.messageId || null,
    source: text(event.payload.source),
    timestamp: event.timestamp,
  };
  return {
    ...state,
    agents: withAgent(state.agents, source),
    checkpoints: [...state.checkpoints, checkpoint],
  };
}

export function reduceAgentEvent(state: AgentEventState, event: RuntimeEvent): AgentEventState {
  if (state.stale || state.appliedEventIds.has(event.eventId)) {
    return state;
  }
  if (state.streamId !== null && state.streamId !== event.streamId) {
    return { ...state, refreshRequested: true, stale: true };
  }
  if (state.lastSequence !== null && event.sequence <= state.lastSequence) {
    return state;
  }
  if (!hasContinuousSequence(state, event)) {
    return { ...state, refreshRequested: true, stale: true };
  }

  let next = state;
  if (event.type === "agent.message.delta" || event.type === "agent.message.completed") {
    next = messageEvent(state, event);
  } else if (event.type.startsWith("tool.")) {
    next = toolEvent(state, event);
  } else if (event.type === "agent.status.changed") {
    next = statusEvent(state, event);
  } else if (event.type === "queue.changed") {
    next = queueEvent(state, event);
  } else if (event.type === "task.status.changed") {
    next = taskEvent(state, event);
  } else if (event.type.startsWith("debug.api.")) {
    next = debugEvent(state, event);
  } else if (event.type === "checkpoint.created") {
    next = checkpointEvent(state, event);
  } else if (event.type === "system.interrupted") {
    next = noticeEvent(state, event, "interrupt");
  } else if (event.type === "system.error") {
    next = noticeEvent(state, event, "error");
  } else if (event.type === "system.notice") {
    next = noticeEvent(state, event, "notice");
  }
  return applied(next, event);
}

export function hydrateAgentState(snapshot: RuntimeSnapshot, streamId: string): AgentEventState {
  let agents: Readonly<Record<string, RuntimeAgentView>> = {};
  for (const [source, status] of Object.entries(snapshot.agent_statuses)) {
    agents = withAgent(agents, source, status);
  }

  const queues: Record<string, RuntimeQueueView> = {};
  for (const queue of snapshot.queues) {
    const id = normalizeAgentId(queue.agent_id);
    agents = withAgent(agents, queue.agent_id);
    queues[id] = {
      agentId: id,
      items: [...queue.queued_messages],
      pendingCount: queue.pending_count,
    };
  }

  const tasks: Record<string, RuntimeTaskView> = {};
  for (const task of snapshot.tasks) {
    const sourceAgent = normalizeAgentId(task.source_agent);
    const targetAgent = normalizeAgentId(task.target_agent);
    agents = withAgent(withAgent(agents, task.source_agent), task.target_agent);
    tasks[task.task_id] = {
      blocking: task.blocking,
      brief: task.brief,
      id: task.task_id,
      sourceAgent,
      status: task.status,
      targetAgent,
    };
  }

  const tools: Record<string, RuntimeToolView> = {};
  for (const tool of snapshot.tools) {
    const agentId = normalizeAgentId(tool.agentId);
    agents = withAgent(agents, tool.agentId);
    tools[tool.toolCallId] = {
      agentId,
      arguments: maskSensitive(tool.arguments),
      error: maskSensitive(tool.error),
      id: tool.toolCallId,
      name: tool.name,
      result: maskSensitive(tool.result),
      status: tool.status,
    };
  }

  let usage: RuntimeUsageView = { durationMs: 0, tokensIn: 0, tokensOut: 0 };
  const debug = snapshot.debug.map((entry, index): RuntimeDebugView => {
    const agentId = normalizeAgentId(entry.agentId);
    agents = withAgent(agents, entry.agentId);
    usage = {
      durationMs: usage.durationMs + entry.duration_ms,
      tokensIn: usage.tokensIn + entry.tokens_in,
      tokensOut: usage.tokensOut + entry.tokens_out,
    };
    return {
      agentId,
      durationMs: entry.duration_ms,
      error: maskSensitive(entry.error),
      id: `snapshot-debug-${index}-${entry.timestamp}`,
      model: text(maskSensitive(entry.model)),
      status: entry.status,
      timestamp: entry.timestamp,
      tokensIn: entry.tokens_in,
      tokensOut: entry.tokens_out,
    };
  });

  const checkpoints = snapshot.checkpoints.map((item): RuntimeCheckpointView => ({
    agentId: normalizeAgentId(item.agent_id),
    description: item.description,
    epoch: item.epoch,
    id: item.checkpoint_id,
    messageId: item.message_id ?? null,
    source: item.source,
    timestamp: item.timestamp,
  }));
  for (const item of snapshot.checkpoints) {
    agents = withAgent(agents, item.agent_id);
  }

  return {
    ...emptyAgentState(streamId),
    agents,
    checkpoints,
    debug,
    queues,
    tasks,
    tools,
    usage,
  };
}
