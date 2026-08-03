import { useState } from "react";

import { ApiError } from "../../api/gateway";
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

export interface MessageListProps {
  readonly agentId: string;
  readonly api: ConversationApi;
  readonly liveMessages?: readonly RuntimeMessageView[] | undefined;
  readonly messages: readonly Record<string, unknown>[];
  readonly onHistoryChanged?: (() => void) | undefined;
  readonly requestEdit?: ((message: ChatMessage) => string | null) | undefined;
}

function newId(): string {
  return createUuid();
}

export function MessageList({
  agentId,
  api,
  liveMessages = [],
  messages,
  onHistoryChanged,
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

  const history = historyOverride?.source === messages
    ? historyOverride.history
    : normalizeMessages(messages);
  const historicalMessageIds = new Set(
    history.flatMap((message) => (
      message.role === "assistant" && message.messageId ? [message.messageId] : []
    )),
  );
  const visibleLiveMessages = liveMessages.filter(
    (message) => message.agentId === agentId.toLowerCase()
      && !historicalMessageIds.has(message.id),
  );

  async function refreshHistory() {
    const refreshed = await api.messages(agentId);
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
    if (!message.checkpointId || !window.confirm(`确定回滚到“${message.content}”？`)) return;
    await runMutation({
      checkpointId: message.checkpointId,
      idempotencyKey: newId(),
      kind: "rollback",
      ...(message.messageId ? { targetMessageId: message.messageId } : {}),
    }, message.id);
  }

  return (
    <section aria-label="消息历史" className="message-list">
      {history.length === 0 && visibleLiveMessages.length === 0 ? <p className="pane-muted">当前会话还没有消息。</p> : (
        <ol>
          {history.map((message) => (
            <li className={`message message--${message.role}`} key={message.id}>
              <header>
                <strong>{message.role === "assistant" ? "Agent" : message.role}</strong>
                {message.timestamp ? <time>{message.timestamp}</time> : null}
              </header>
              <p>{message.content}</p>
              {message.role === "user" && message.messageId ? (
                <button
                  disabled={pendingId !== null}
                  onClick={() => void editAndResend(message)}
                  type="button"
                >编辑并重发 {message.content}</button>
              ) : null}
              {message.role === "checkpoint" && message.checkpointId ? (
                <button
                  disabled={pendingId !== null}
                  onClick={() => void rollback(message)}
                  type="button"
                >回滚 {message.content}</button>
              ) : null}
            </li>
          ))}
          {visibleLiveMessages.map((message) => (
            <li className="message message--assistant message--live" key={`live-${message.id}`}>
              <header>
                <strong>{message.agentId}</strong>
                <time>{message.timestamp}</time>
              </header>
              <p>{message.content || "正在生成…"}</p>
              <small>{message.status === "streaming" ? "流式生成中" : "已完成"}</small>
            </li>
          ))}
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
