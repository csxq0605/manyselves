import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { ApiGateway } from "../../api/gateway";
import type { RuntimeMessageView } from "../agents/event-reducer";
import { MessageComposer } from "../chat/MessageComposer";
import { MessageList } from "../chat/MessageList";
import { createConversationMessageStore } from "../chat/message-store";
import { createFileApi } from "../files/file-api";
import { ConversationActions } from "./ConversationActions";
import { createConversationApi, type ConversationListSnapshot } from "./conversation-api";
import { ConversationList } from "./ConversationList";

export interface ConversationWorkspaceProps {
  readonly agentId: string;
  readonly gateway: ApiGateway;
  readonly liveMessages?: readonly RuntimeMessageView[] | undefined;
  readonly onSessionChanged?: ((sessionId: string) => void) | undefined;
  readonly projectId: string;
  readonly projectName?: string | undefined;
  readonly requestedSessionId?: string | undefined;
}

export function ConversationWorkspace({
  agentId,
  gateway,
  liveMessages = [],
  onSessionChanged,
  projectId,
  projectName,
  requestedSessionId,
}: ConversationWorkspaceProps) {
  const client = useQueryClient();
  const api = useMemo(() => createConversationApi(gateway), [gateway]);
  const fileApi = useMemo(() => createFileApi(gateway), [gateway]);
  const [messageStore] = useState(() => createConversationMessageStore());
  const activationAttemptRef = useRef<{ readonly key: string; readonly promise: ReturnType<typeof api.activate> } | null>(null);
  const [activationError, setActivationError] = useState<string | null>(null);
  const [overflowOpen, setOverflowOpen] = useState(false);
  const conversations = useQuery({
    queryFn: () => api.list(projectId, agentId),
    queryKey: ["conversations", projectId, agentId],
  });
  const activeSessionId = conversations.data?.activeSessionId ?? null;
  const sessionReady = !requestedSessionId || requestedSessionId === activeSessionId;
  const messages = useQuery({
    enabled: activeSessionId !== null && sessionReady,
    queryFn: () => api.messages(projectId, agentId),
    queryKey: ["conversation-messages", projectId, agentId, activeSessionId],
  });
  const activeConversation = conversations.data?.conversations.find(
    (item) => item.sessionId === activeSessionId,
  ) ?? null;
  const activeLiveMessages = liveMessages.filter(
    (message) => message.sessionId === null || message.sessionId === activeSessionId,
  );
  const requestedSessionMissing = Boolean(
    requestedSessionId
    && conversations.data
    && !conversations.data.conversations.some((item) => item.sessionId === requestedSessionId),
  );
  const hasMessages = Boolean(messages.data?.messages.length || activeLiveMessages.length);

  useEffect(() => {
    if (!requestedSessionId || !conversations.data || requestedSessionId === activeSessionId) return;
    if (!conversations.data.conversations.some((item) => item.sessionId === requestedSessionId)) return;
    const attemptKey = `${projectId}:${requestedSessionId}`;
    if (!activationAttemptRef.current || activationAttemptRef.current.key !== attemptKey) {
      activationAttemptRef.current = {
        key: attemptKey,
        promise: api.activate(projectId, requestedSessionId, agentId),
      };
    }
    const attempt = activationAttemptRef.current;
    let cancelled = false;
    void attempt.promise.then((activated) => {
      if (cancelled) return;
      setActivationError(null);
      client.setQueryData<ConversationListSnapshot>(["conversations", projectId, agentId], (current) => current ? {
        ...current,
        activeSessionId: activated.sessionId,
        conversations: current.conversations.map((item) => ({
          ...item,
          active: item.sessionId === activated.sessionId,
        })),
      } : current);
      void client.invalidateQueries({ queryKey: ["conversation-messages", projectId, agentId] });
    }).catch(() => {
      if (!cancelled) setActivationError("会话切换失败");
    }).finally(() => {
      if (activationAttemptRef.current === attempt) activationAttemptRef.current = null;
    });
    return () => { cancelled = true; };
  }, [activeSessionId, agentId, api, client, conversations.data, projectId, requestedSessionId]);

  function refresh(activeId?: string) {
    if (activeId) {
      client.setQueryData<ConversationListSnapshot>(["conversations", projectId, agentId], (current) => current ? {
        ...current,
        activeSessionId: activeId,
        conversations: current.conversations.map((item) => ({
          ...item,
          active: item.sessionId === activeId,
        })),
      } : current);
      onSessionChanged?.(activeId);
    }
    void client.invalidateQueries({ queryKey: ["conversations", projectId, agentId] });
    void client.invalidateQueries({ queryKey: ["conversation-messages", projectId, agentId] });
  }

  return (
    <section aria-label="对话区域" className="conversation-workspace">
      <div className="conversation-workspace__main">
        {requestedSessionMissing ? <p role="alert">该会话不属于当前项目</p> : null}
        {activationError ? <p role="alert">{activationError}</p> : null}
        {requestedSessionId && conversations.data && !requestedSessionMissing && !sessionReady ? <p role="status">正在切换会话…</p> : null}
        {sessionReady && activeSessionId && messages.isPending ? <p role="status">正在加载会话历史…</p> : null}
        {sessionReady && messages.isError ? <p role="alert">会话历史加载失败</p> : null}
        {sessionReady && messages.data && !hasMessages ? <div className="conversation-empty">
          <span aria-hidden="true" className="conversation-empty__spark">✦</span>
          <h1>今天要处理什么？</h1>
          <p>当前对话属于“{projectName ?? projectId}”项目。Agent 会自动使用项目输入、知识库与输出模板。</p>
        </div> : null}
        {sessionReady && messages.data && hasMessages ? (
          <MessageList
            agentId={agentId}
            api={api}
            liveMessages={activeLiveMessages}
            messages={messages.data.messages}
            onHistoryChanged={() => void messages.refetch()}
            projectId={projectId}
          />
        ) : null}
      </div>
      {activeSessionId && sessionReady && !requestedSessionMissing ? (
        <MessageComposer
          agentId={agentId}
          api={api}
          fileApi={fileApi}
          key={`${projectId}:${activeSessionId}`}
          onHistoryRequested={() => void messages.refetch()}
          onSessionChanged={refresh}
          projectId={projectId}
          sessionId={activeSessionId}
          store={messageStore}
        />
      ) : null}
      <div className="conversation-overflow">
        <button aria-expanded={overflowOpen} aria-label="更多会话操作" onClick={() => setOverflowOpen((current) => !current)} type="button">•••</button>
        {overflowOpen ? <div className="conversation-overflow__panel">
          <ConversationList
            agentId={agentId}
            api={api}
            onActivated={refresh}
            projectId={projectId}
          />
          <ConversationActions
            activeConversation={activeConversation}
            agentId={agentId}
            api={api}
            onChanged={refresh}
            projectId={projectId}
          />
        </div> : null}
      </div>
    </section>
  );
}
