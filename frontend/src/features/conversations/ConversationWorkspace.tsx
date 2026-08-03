import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import type { ApiGateway } from "../../api/gateway";
import type { RuntimeMessageView } from "../agents/event-reducer";
import type { SelectionInput } from "../editor/selection-context";
import { createFileApi } from "../files/file-api";
import { MessageComposer } from "../chat/MessageComposer";
import { MessageList } from "../chat/MessageList";
import { createConversationMessageStore } from "../chat/message-store";
import { ConversationActions } from "./ConversationActions";
import { createConversationApi } from "./conversation-api";
import { ConversationList } from "./ConversationList";

export interface ConversationWorkspaceProps {
  readonly agentId: string;
  readonly currentEditorPath?: string | null | undefined;
  readonly currentSelection?: SelectionInput | null | undefined;
  readonly gateway: ApiGateway;
  readonly liveMessages?: readonly RuntimeMessageView[] | undefined;
  readonly projectId: string;
}

export function ConversationWorkspace({
  agentId,
  currentEditorPath,
  currentSelection,
  gateway,
  liveMessages = [],
  projectId,
}: ConversationWorkspaceProps) {
  const client = useQueryClient();
  const api = useMemo(() => createConversationApi(gateway), [gateway]);
  const fileApi = useMemo(() => createFileApi(gateway), [gateway]);
  const [messageStore] = useState(() => createConversationMessageStore());
  const conversations = useQuery({
    queryFn: () => api.list(agentId),
    queryKey: ["conversations", projectId, agentId],
  });
  const activeSessionId = conversations.data?.activeSessionId ?? null;
  const messages = useQuery({
    enabled: activeSessionId !== null,
    queryFn: () => api.messages(agentId),
    queryKey: ["conversation-messages", projectId, agentId, activeSessionId],
  });
  const files = useQuery({
    queryFn: () => fileApi.listTree(projectId),
    queryKey: ["files", projectId],
  });
  const availableFiles = (files.data ?? [])
    .filter((entry) => entry.kind === "file")
    .map((entry) => entry.path);
  const activeConversation = conversations.data?.conversations.find(
    (item) => item.sessionId === activeSessionId,
  ) ?? null;
  const activeLiveMessages = liveMessages.filter(
    (message) => message.sessionId === null || message.sessionId === activeSessionId,
  );

  function refresh(activeId?: string) {
    if (activeId) {
      client.setQueryData(["conversations", projectId, agentId], (current: typeof conversations.data) => current ? {
        ...current,
        activeSessionId: activeId,
        conversations: current.conversations.map((item) => ({
          ...item,
          active: item.sessionId === activeId,
        })),
      } : current);
    }
    void client.invalidateQueries({ queryKey: ["conversations", projectId, agentId] });
    void client.invalidateQueries({ queryKey: ["conversation-messages", projectId, agentId] });
  }

  return (
    <section aria-label="对话区域" className="conversation-workspace">
      <div className="conversation-workspace__header">
        <div>
          <p className="pane-label">对话</p>
          <h2>{activeConversation?.name ?? "Agent 会话"}</h2>
        </div>
        <ConversationActions
          activeConversation={activeConversation}
          agentId={agentId}
          api={api}
          onChanged={refresh}
        />
      </div>
      <ConversationList agentId={agentId} api={api} cacheScope={projectId} onActivated={() => refresh()} />
      {messages.isPending ? <p role="status">正在加载会话历史…</p> : null}
      {messages.isError ? <p role="alert">会话历史加载失败</p> : null}
      {messages.data ? (
        <MessageList
          agentId={agentId}
          api={api}
          liveMessages={activeLiveMessages}
          messages={messages.data.messages}
          onHistoryChanged={() => void messages.refetch()}
        />
      ) : null}
      {activeSessionId ? (
        <MessageComposer
          agentId={agentId}
          api={api}
          availableFiles={availableFiles}
          currentEditorPath={currentEditorPath}
          currentSelection={currentSelection}
          key={`${projectId}:${activeSessionId}`}
          onHistoryRequested={() => void messages.refetch()}
          onSessionChanged={refresh}
          sessionId={activeSessionId}
          store={messageStore}
        />
      ) : null}
    </section>
  );
}
