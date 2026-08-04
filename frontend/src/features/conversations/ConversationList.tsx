import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type { ConversationApi, ConversationListSnapshot } from "./conversation-api";

export interface ConversationListProps {
  readonly agentId: string;
  readonly api: ConversationApi;
  readonly onActivated?: ((sessionId: string) => void) | undefined;
  readonly projectId: string;
}

function queryKey(projectId: string, agentId: string) {
  return ["conversations", projectId, agentId] as const;
}

export function ConversationList({ agentId, api, onActivated, projectId }: ConversationListProps) {
  const client = useQueryClient();
  const conversations = useQuery({
    queryFn: () => api.list(projectId, agentId),
    queryKey: queryKey(projectId, agentId),
  });
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const activation = useMutation({
    mutationFn: (sessionId: string) => api.activate(projectId, sessionId, agentId),
    onError: () => {
      setError("会话切换失败");
      setNotice(null);
    },
    onSuccess: (activated) => {
      setError(null);
      setNotice(`${activated.name} 已激活`);
      client.setQueryData<ConversationListSnapshot>(queryKey(projectId, agentId), (current) => current ? {
        ...current,
        activeSessionId: activated.sessionId,
        conversations: current.conversations.map((item) => ({
          ...item,
          active: item.sessionId === activated.sessionId,
        })),
      } : current);
      onActivated?.(activated.sessionId);
    },
  });

  if (conversations.isPending) return <p>正在加载会话…</p>;
  if (conversations.isError) return <p role="alert">会话加载失败</p>;
  const activeSessionId = conversations.data.activeSessionId;

  return (
    <section aria-label="会话列表" className="conversation-list">
      <ul>
        {conversations.data.conversations.map((conversation) => (
          <li key={conversation.sessionId}>
            <button
              aria-current={conversation.sessionId === activeSessionId ? "true" : undefined}
              disabled={activation.isPending}
              onClick={() => {
                if (conversation.sessionId === activeSessionId) return;
                setError(null);
                setNotice(null);
                activation.mutate(conversation.sessionId);
              }}
              type="button"
            >
              <span>{conversation.name}</span>
              {conversation.preview ? <small>{conversation.preview}</small> : null}
            </button>
          </li>
        ))}
      </ul>
      {notice ? <p aria-live="polite">{notice}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}
