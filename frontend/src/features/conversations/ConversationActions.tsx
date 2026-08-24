import { type FormEvent, useState } from "react";

import type { ConversationApi, ConversationSummary } from "./conversation-api";

type ConversationAction = "clear" | "create" | "delete" | "rename";

export interface ConversationActionsProps {
  readonly activeConversation: ConversationSummary | null;
  readonly agentId: string;
  readonly api: ConversationApi;
  readonly onChanged?: ((activeSessionId?: string, conversation?: ConversationSummary) => void) | undefined;
  readonly projectId: string;
}

export function ConversationActions({
  activeConversation,
  agentId,
  api,
  onChanged,
  projectId,
}: ConversationActionsProps) {
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<ConversationAction | null>(null);
  const [name, setName] = useState("");
  const [pending, setPending] = useState(false);

  async function run(action: () => Promise<ConversationSummary | string | undefined>, failure: string) {
    setPending(true);
    setError(null);
    try {
      const result = await action();
      setMode(null);
      onChanged?.(
        typeof result === "string" ? result : result?.sessionId,
        typeof result === "string" ? undefined : result,
      );
    } catch {
      setError(failure);
    } finally {
      setPending(false);
    }
  }

  function open(nextMode: ConversationAction) {
    setError(null);
    setMode(nextMode);
    setName(nextMode === "rename" ? activeConversation?.name ?? "" : "");
  }

  function cancel() {
    if (pending) return;
    setError(null);
    setMode(null);
  }

  function submitName(event: FormEvent) {
    event.preventDefault();
    const normalized = name.trim();
    if (!normalized) {
      setError("请输入会话名称");
      return;
    }
    if (mode === "create") {
      void run(() => api.create(projectId, normalized, agentId), "新建会话失败");
      return;
    }
    if (mode === "rename" && activeConversation) {
      void run(
        () => api.rename(projectId, activeConversation.sessionId, normalized, agentId),
        "重命名失败",
      );
    }
  }

  function confirmDestructiveAction() {
    if (!activeConversation) return;
    if (mode === "delete") {
      void run(
        async () => (await api.delete(projectId, activeConversation.sessionId, agentId)).activeSessionId,
        "删除会话失败",
      );
    } else if (mode === "clear") {
      void run(async () => (await api.clear(projectId, agentId)).activeSessionId, "清空会话失败");
    }
  }

  return (
    <div aria-label="会话操作" className="conversation-actions" role="group">
      {mode === null ? <>
        <button disabled={pending} onClick={() => open("create")} type="button">新建会话</button>
        <button disabled={pending || !activeConversation} onClick={() => open("rename")} type="button">重命名会话</button>
        <button disabled={pending || !activeConversation} onClick={() => open("delete")} type="button">删除会话</button>
        <button disabled={pending || !activeConversation} onClick={() => open("clear")} type="button">清空当前会话</button>
      </> : null}
      {mode === "create" || mode === "rename" ? <form
        aria-label={mode === "create" ? "新建会话" : "重命名会话"}
        className="conversation-actions__editor"
        onSubmit={submitName}
      >
        <label>会话名称<input autoComplete="off" autoFocus disabled={pending} onChange={(event) => setName(event.target.value)} value={name} /></label>
        <div className="conversation-actions__buttons">
          <button disabled={pending} type="submit">{mode === "create" ? "确认新建" : "确认重命名"}</button>
          <button disabled={pending} onClick={cancel} type="button">取消</button>
        </div>
      </form> : null}
      {mode === "delete" || mode === "clear" ? <div
        aria-label={mode === "delete" ? "删除会话确认" : "清空会话确认"}
        className="conversation-actions__editor"
        role="group"
      >
        <strong>{mode === "delete" ? "删除会话" : "清空当前会话"}</strong>
        <p>{mode === "delete"
          ? `确定删除“${activeConversation?.name ?? "当前会话"}”？此操作无法撤销。`
          : `确定清空“${activeConversation?.name ?? "当前会话"}”的全部消息？此操作无法撤销。`}</p>
        <div className="conversation-actions__buttons">
          <button className="conversation-actions__danger" disabled={pending} onClick={confirmDestructiveAction} type="button">{mode === "delete" ? "确认删除" : "确认清空"}</button>
          <button disabled={pending} onClick={cancel} type="button">{mode === "delete" ? "取消删除" : "取消清空"}</button>
        </div>
      </div> : null}
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}
