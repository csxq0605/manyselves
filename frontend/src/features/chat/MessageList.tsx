import { useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/gateway";
import { formatShanghaiDateTime } from "../../app/date-time";
import { createUuid } from "../../app/uuid";
import type { RuntimeMessageView } from "../agents/event-reducer";
import type { ConversationApi } from "../conversations/conversation-api";
import { normalizeMessages, type ChatMessage } from "./message-model";

interface EditAttempt {
  readonly content: string;
  readonly idempotencyKey: string;
  readonly kind: "edit";
  readonly messageId: string;
  readonly targetMessageId: string;
}

interface RollbackAttempt {
  readonly checkpointId: string;
  readonly idempotencyKey: string;
  readonly kind: "rollback";
  readonly targetMessageId?: string | undefined;
}

type HistoryMutationAttempt = EditAttempt | RollbackAttempt;

export interface RuntimeTaskSummary {
  readonly agentPath: string;
  readonly brief: string;
  readonly status: string;
}

export interface MessageListRuntimeSummary {
  readonly activeTasks: number;
  readonly agentStatuses: Readonly<Record<string, string>>;
  readonly completedTasks: number;
  readonly currentTasks: readonly RuntimeTaskSummary[];
  readonly ready: boolean;
  readonly runningTools: number;
  readonly totalTasks: number;
}

export interface MessageListProps {
  readonly agentId: string;
  readonly api: ConversationApi;
  readonly liveMessages?: readonly RuntimeMessageView[] | undefined;
  readonly messages: readonly Record<string, unknown>[];
  readonly onHistoryChanged?: (() => void) | undefined;
  readonly projectId: string;
  readonly requestEdit?: ((message: ChatMessage) => string | null) | undefined;
  readonly runtimeSummary?: MessageListRuntimeSummary | undefined;
}

function newId(): string {
  return createUuid();
}

function messageRoleLabel(message: ChatMessage): string {
  return message.role === "assistant" ? "Agent" : message.role;
}

function messageMeta(message: ChatMessage) {
  const role = messageRoleLabel(message);
  if (!message.timestamp) return <strong>{role}</strong>;
  const timestamp = formatShanghaiDateTime(message.timestamp);
  return message.role === "user"
    ? <><time dateTime={message.timestamp}>{timestamp}</time>{" "}<strong>{role}</strong></>
    : <><strong>{role}</strong>{" "}<time dateTime={message.timestamp}>{timestamp}</time></>;
}

function isPreCheckpoint(message: ChatMessage): boolean {
  return message.role === "checkpoint"
    && message.content.startsWith("pre:")
    && Boolean(message.checkpointId);
}

function isRuntimeTrace(message: ChatMessage): boolean {
  return message.role === "tool_call" || message.role === "tool_result";
}

function runtimeTraceLabel(message: ChatMessage): string {
  return message.role === "tool_call" ? "tool_call" : "tool_result";
}

export function MessageList({
  agentId,
  api,
  liveMessages = [],
  messages,
  onHistoryChanged,
  projectId,
  runtimeSummary,
  requestEdit = (message) => window.prompt("编辑消息后重新发送", message.content),
}: MessageListProps) {
  const [historyOverride, setHistoryOverride] = useState<{
    readonly history: ChatMessage[];
    readonly source: readonly Record<string, unknown>[];
  } | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [failedAttempt, setFailedAttempt] = useState<HistoryMutationAttempt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [runtimeOpen, setRuntimeOpen] = useState(false);
  const latestMessageRef = useRef<HTMLLIElement | null>(null);

  const history = historyOverride?.source === messages
    ? historyOverride.history
    : normalizeMessages(messages);
  const preCheckpointsByMessageId = new Map<string, ChatMessage>();
  const visibleHistory = history.filter((message) => {
    if (isRuntimeTrace(message)) return false;
    if (!isPreCheckpoint(message)) return true;
    if (message.messageId) preCheckpointsByMessageId.set(message.messageId, message);
    return false;
  });
  const runtimeTraces = history.filter(isRuntimeTrace);
  const showRuntimePanel = runtimeTraces.length > 0 || runtimeSummary !== undefined;
  const historicalMessageIds = new Set(
    history.flatMap((message) => (
      message.role === "assistant" && message.messageId ? [message.messageId] : []
    )),
  );
  const visibleLiveMessages = liveMessages.filter(
    (message) => message.agentId === agentId.toLowerCase()
      && !historicalMessageIds.has(message.id),
  );
  const latestHistoryMessage = visibleHistory.at(-1);
  const latestLiveMessage = visibleLiveMessages.at(-1);
  const latestMessageKey = [
    latestHistoryMessage?.id ?? "",
    latestHistoryMessage?.content ?? "",
    latestLiveMessage?.id ?? "",
    latestLiveMessage?.content ?? "",
  ].join(":");

  useEffect(() => {
    latestMessageRef.current?.scrollIntoView?.({ block: "end" });
  }, [latestMessageKey]);

  async function refreshHistory() {
    const refreshed = await api.messages(projectId, agentId);
    setHistoryOverride({ history: normalizeMessages(refreshed.messages), source: messages });
    onHistoryChanged?.();
  }

  async function runMutation(attempt: HistoryMutationAttempt, pendingMarker: string) {
    setPendingId(pendingMarker);
    setError(null);
    setNotice(null);
    try {
      if (attempt.kind === "edit") {
        await api.editResend(
          projectId,
          agentId,
          attempt.targetMessageId,
          attempt.content,
          attempt.idempotencyKey,
          attempt.messageId,
        );
        await refreshHistory();
        setNotice("历史已刷新");
      } else {
        const restored = await api.rollback(
          projectId,
          agentId,
          attempt.checkpointId,
          attempt.idempotencyKey,
          attempt.targetMessageId,
        );
        setHistoryOverride({ history: normalizeMessages(restored.conversationHistory), source: messages });
        setNotice(`已恢复 ${restored.restoredFiles} 个文件，历史已刷新`);
        onHistoryChanged?.();
      }
      setFailedAttempt(null);
    } catch (reason) {
      if (!(reason instanceof ApiError) || reason.retryable) setFailedAttempt(attempt);
      try {
        await refreshHistory();
        setError(reason instanceof ApiError
          ? "操作被服务器拒绝，已刷新服务器历史"
          : "操作结果未知，已刷新服务器历史；可使用相同请求重试");
      } catch {
        setError(reason instanceof ApiError
          ? "操作被服务器拒绝，但历史刷新失败"
          : "操作结果未知且历史刷新失败；恢复连接后可使用相同请求重试");
      }
    } finally {
      setPendingId(null);
    }
  }

  function editAndResend(message: ChatMessage) {
    if (!message.messageId) return;
    const content = requestEdit(message)?.trim();
    if (!content) return;
    void runMutation({
      content,
      idempotencyKey: newId(),
      kind: "edit",
      messageId: newId(),
      targetMessageId: message.messageId,
    }, message.id);
  }

  async function rollback(message: ChatMessage) {
    if (!message.checkpointId) return;
    await rollbackToCheckpoint(
      message.checkpointId,
      message.content,
      message.id,
      message.messageId ?? undefined,
    );
  }

  async function rollbackToCheckpoint(
    checkpointId: string,
    label: string,
    pendingMarker: string,
    targetMessageId?: string,
  ) {
    if (!window.confirm(`确定回滚到“${label}”？`)) return;
    await runMutation({
      checkpointId,
      idempotencyKey: newId(),
      kind: "rollback",
      ...(targetMessageId ? { targetMessageId } : {}),
    }, pendingMarker);
  }

  return (
    <section aria-label="消息历史" className="message-list">
      {showRuntimePanel ? (
        <aside aria-label="Agent 运行态" className={`agent-runtime${runtimeOpen ? " agent-runtime--open" : ""}`}>
          <button
            aria-expanded={runtimeOpen}
            aria-label={runtimeOpen ? "折叠 Agent 运行态" : "展开 Agent 运行态"}
            className="agent-runtime__toggle"
            onClick={() => setRuntimeOpen((current) => !current)}
            type="button"
          >
            <span aria-hidden="true">⚙</span>
            <strong>Agent 运行态</strong>
            {runtimeSummary ? (
              <>
                <span className="agent-runtime__pill">{runtimeSummary.activeTasks} 进行中</span>
                <span className="agent-runtime__pill">{runtimeSummary.completedTasks}/{runtimeSummary.totalTasks} 完成</span>
              </>
            ) : (
              <span className="agent-runtime__count">{runtimeTraces.length}</span>
            )}
          </button>
          {runtimeOpen ? (
            <div className="agent-runtime__panel">
              {runtimeSummary ? (
                <div className="agent-runtime__summary">
                  <span className={`agent-runtime__ready agent-runtime__ready--${runtimeSummary.ready ? "ready" : "offline"}`}>
                    {runtimeSummary.ready ? "服务就绪" : "服务离线"}
                  </span>
                  <span>{runtimeSummary.runningTools} 个工具运行中</span>
                </div>
              ) : null}
              {runtimeSummary ? (
                <div className="agent-runtime__section">
                  <h3>Agent 状态</h3>
                  <ul className="agent-runtime__mini-list" aria-label="Agent 状态">
                    {Object.entries(runtimeSummary.agentStatuses).map(([runtimeAgentId, status]) => (
                      <li key={runtimeAgentId}>
                        <strong>{runtimeAgentId}</strong>
                        <span>{status}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {runtimeSummary && runtimeSummary.currentTasks.length > 0 ? (
                <div className="agent-runtime__section">
                  <h3>当前任务</h3>
                  <ul className="agent-runtime__mini-list" aria-label="当前任务">
                    {runtimeSummary.currentTasks.map((task) => (
                      <li key={`${task.agentPath}:${task.brief}`}>
                        <strong>{task.brief}</strong>
                        <span>{task.agentPath} · {task.status}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {runtimeTraces.length > 0 ? (
                <ol aria-label="运行态记录" className="agent-runtime__list">
                  {runtimeTraces.map((message) => (
                    <li key={message.id}>
                      <time dateTime={message.timestamp ?? undefined}>
                        {message.timestamp ? formatShanghaiDateTime(message.timestamp) : "—"}
                      </time>
                      <strong>{runtimeTraceLabel(message)}</strong>
                      <span>{message.content || "—"}</span>
                    </li>
                  ))}
                </ol>
              ) : null}
            </div>
          ) : null}
        </aside>
      ) : null}
      {visibleHistory.length === 0 && visibleLiveMessages.length === 0 ? <p className="pane-muted">当前会话还没有消息。</p> : (
        <ol>
          {visibleHistory.map((message) => {
            const attachedCheckpoint = message.messageId ? preCheckpointsByMessageId.get(message.messageId) : undefined;
            const attachedCheckpointId = attachedCheckpoint?.checkpointId;
            return (
              <li className={`message message--${message.role}`} key={message.id}>
                <header className="message__meta">
                  {messageMeta(message)}
                </header>
                <p className="message__bubble">{message.content}</p>
                {(message.role === "user" && message.messageId) || attachedCheckpointId ? (
                  <div className="message__actions">
                    {message.role === "user" && message.messageId ? (
                      <button
                        aria-label={`重编辑并重发 ${message.content}`}
                        className="message__action"
                        disabled={pendingId !== null}
                        onClick={() => void editAndResend(message)}
                        type="button"
                      ><span aria-hidden="true">✎</span> 重编辑</button>
                    ) : null}
                    {attachedCheckpoint && attachedCheckpointId ? (
                      <button
                        aria-label={`回滚到此处：${message.content}`}
                        className="message__action message__action--rollback"
                        disabled={pendingId !== null}
                        onClick={() => void rollbackToCheckpoint(
                          attachedCheckpointId,
                          message.content,
                          attachedCheckpoint.id,
                          message.messageId ?? undefined,
                        )}
                        type="button"
                      ><span aria-hidden="true">↩</span> 回滚到此处</button>
                    ) : null}
                  </div>
                ) : null}
                {message.role === "checkpoint" && message.checkpointId ? (
                  <button
                    disabled={pendingId !== null}
                    onClick={() => void rollback(message)}
                    type="button"
                  >回滚 {message.content}</button>
                ) : null}
              </li>
            );
          })}
          {visibleLiveMessages.map((message) => (
            <li className="message message--assistant message--live" key={`live-${message.id}`}>
              <header className="message__meta">
                <strong>{message.agentId}</strong>
                <time dateTime={message.timestamp}>{formatShanghaiDateTime(message.timestamp)}</time>
              </header>
              <p className="message__bubble">{message.content || "正在生成…"}</p>
              <small>{message.status === "streaming" ? "流式生成中" : "已完成"}</small>
            </li>
          ))}
          <li aria-hidden="true" className="message-list__end" ref={latestMessageRef} />
        </ol>
      )}
      {failedAttempt ? (
        <button
          disabled={pendingId !== null}
          onClick={() => void runMutation(failedAttempt, `retry-${failedAttempt.kind}`)}
          type="button"
        >{failedAttempt.kind === "edit" ? "重试编辑重发" : "重试回滚"}</button>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}
