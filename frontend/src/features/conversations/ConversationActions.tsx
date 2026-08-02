import { useState } from "react";

import type { ConversationApi, ConversationSummary } from "./conversation-api";

type NameAction = "create" | "rename";

export interface ConversationActionsProps {
  readonly activeConversation: ConversationSummary | null;
  readonly agentId: string;
  readonly api: ConversationApi;
  readonly onChanged?: ((activeSessionId?: string) => void) | undefined;
  readonly requestName?: ((action: NameAction) => string | null) | undefined;
}

export function ConversationActions({
  activeConversation,
  agentId,
  api,
  onChanged,
  requestName = (action) => window.prompt(action === "create" ? "请输入会话名称" : "请输入新名称"),
}: ConversationActionsProps) {
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function run(action: () => Promise<string | undefined>, failure: string) {
    setPending(true);
    setError(null);
    try {
      const activeSessionId = await action();
      onChanged?.(activeSessionId);
    } catch {
      setError(failure);
    } finally {
      setPending(false);
    }
  }

  function createConversation() {
    const name = requestName("create")?.trim();
    if (!name) return;
    void run(async () => (await api.create(name, agentId)).sessionId, "新建会话失败");
  }

  function renameConversation() {
    if (!activeConversation) return;
    const name = requestName("rename")?.trim();
    if (!name) return;
    void run(async () => (await api.rename(activeConversation.sessionId, name, agentId)).sessionId, "重命名失败");
  }

  function deleteConversation() {
    if (!activeConversation || !window.confirm(`确定删除会话“${activeConversation.name}”？`)) return;
    void run(async () => (await api.delete(activeConversation.sessionId, agentId)).activeSessionId, "删除会话失败");
  }

  function clearConversation() {
    if (!activeConversation || !window.confirm(`确定清空会话“${activeConversation.name}”？`)) return;
    void run(async () => (await api.clear(agentId)).activeSessionId, "清空会话失败");
  }

  return (
    <div aria-label="会话操作" className="conversation-actions" role="group">
      <button disabled={pending} onClick={createConversation} type="button">新建会话</button>
      <button disabled={pending || !activeConversation} onClick={renameConversation} type="button">重命名会话</button>
      <button disabled={pending || !activeConversation} onClick={deleteConversation} type="button">删除会话</button>
      <button disabled={pending || !activeConversation} onClick={clearConversation} type="button">清空当前会话</button>
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}
