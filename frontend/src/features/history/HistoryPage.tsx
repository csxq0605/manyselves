import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import type { ApiGateway } from "../../api/gateway";
import { formatShanghaiDateTime } from "../../app/date-time";
import { useConversationStore } from "../../store/conversation-store";
import { MessageList } from "../chat/MessageList";
import { createConversationApi } from "../conversations/conversation-api";
import { cacheCreatedConversation } from "../conversations/conversation-cache";
import "../conversations/conversation.css";
import "./history-page.css";

const historyPageSize = 20;

const copy = {
  active: "\u5f53\u524d",
  createFirst: "\u521b\u5efa\u7b2c\u4e00\u4e2a\u5bf9\u8bdd",
  empty: "\u8fd8\u6ca1\u6709\u5386\u53f2\u4f1a\u8bdd",
  historyLoadFailed: "\u4f1a\u8bdd\u5217\u8868\u52a0\u8f7d\u5931\u8d25",
  historyRouteInvalid: "\u5386\u53f2\u4f1a\u8bdd\u8def\u7531\u65e0\u6548",
  loadingHistory: "\u6b63\u5728\u52a0\u8f7d\u4f1a\u8bdd\u2026",
  newConversation: "\u65b0\u5bf9\u8bdd",
  nextPage: "\u4e0b\u4e00\u9875",
  previousPage: "\u4e0a\u4e00\u9875",
  readOnly: "只读：这不是服务当前绑定的项目，可以查看历史对话，不能继续对话。",
  backToHistory: "← 返回历史会话",
  sessionLoadFailed: "会话内容加载失败",
  loadingSession: "正在加载会话内容…",
  reload: "\u91cd\u65b0\u52a0\u8f7d",
  title: "\u5386\u53f2\u4f1a\u8bdd",
};

export interface HistoryPageProps {
  readonly gateway: ApiGateway;
  readonly projectId: string;
}

export function HistoryPage({ gateway, projectId }: HistoryPageProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const api = useMemo(() => createConversationApi(gateway), [gateway]);
  const setActiveSession = useConversationStore((state) => state.setActiveSession);
  const [pagination, setPagination] = useState({ page: 0, projectId });

  const conversations = useQuery({
    queryFn: () => api.list(projectId, "main"),
    queryKey: ["conversations", projectId, "main"],
  });
  const page = pagination.projectId === projectId ? pagination.page : 0;
  const allConversations = conversations.data?.conversations ?? [];
  const totalPages = Math.max(1, Math.ceil(allConversations.length / historyPageSize));
  const currentPage = Math.min(page, totalPages - 1);
  const visibleConversations = allConversations.slice(
    currentPage * historyPageSize,
    currentPage * historyPageSize + historyPageSize,
  );

  function updatePage(nextPage: (value: number) => number) {
    setPagination({ page: nextPage(page), projectId });
  }

  const readOnly = conversations.data?.readOnly === true;

  async function activateSession(sessionId: string) {
    if (readOnly) {
      // Another project: open the stored conversation to read, switch nothing.
      navigate(`/projects/${encodeURIComponent(projectId)}/history/${encodeURIComponent(sessionId)}`);
      return;
    }
    try {
      await api.activate(projectId, sessionId, "main");
      setActiveSession(projectId, sessionId);
      navigate(`/projects/${encodeURIComponent(projectId)}/conversations/${sessionId}`);
    } catch (error) {
      console.error("Failed to activate session:", error);
    }
  }

  async function createNewConversation() {
    try {
      const created = await api.create(projectId, copy.newConversation, "main");
      await cacheCreatedConversation(queryClient, projectId, "main", created);
      setActiveSession(projectId, created.sessionId);
      navigate(`/projects/${encodeURIComponent(projectId)}/conversations/${created.sessionId}`);
    } catch (error) {
      console.error("Failed to create conversation:", error);
    }
  }

  return (
    <section className="history-page">
      <header className="history-page__header">
        <div>
          <p className="history-page__eyebrow">PROJECT / {projectId}</p>
          <h1>{copy.title}</h1>
        </div>
        {readOnly ? null : (
          <button
            className="history-page__new-button"
            onClick={() => void createNewConversation()}
            type="button"
          >
            {copy.newConversation}
          </button>
        )}
      </header>
      {readOnly ? <p className="history-page__read-only" role="note">{copy.readOnly}</p> : null}

      {conversations.isPending ? (
        <p role="status">{copy.loadingHistory}</p>
      ) : conversations.isError ? (
        <div>
          <p role="alert">{copy.historyLoadFailed}</p>
          <button onClick={() => void conversations.refetch()} type="button">
            {copy.reload}
          </button>
        </div>
      ) : conversations.data.conversations.length === 0 ? (
        <div className="history-page__empty">
          <p>{copy.empty}</p>
          {readOnly ? null : (
            <button onClick={() => void createNewConversation()} type="button">
              {copy.createFirst}
            </button>
          )}
        </div>
      ) : (
        <div className="history-page__panel">
          <div className="history-page__viewport">
            <ul aria-label="history-conversation-list" className="history-page__list">
              {visibleConversations.map((conversation) => (
                <li key={conversation.sessionId} className="history-page__item">
                  <button
                    className={`history-page__item-button ${conversation.active ? "history-page__item-button--active" : ""}`}
                    onClick={() => void activateSession(conversation.sessionId)}
                    type="button"
                  >
                    <div className="history-page__item-content">
                      <h2 className="history-page__item-name">{conversation.name}</h2>
                      {conversation.preview ? (
                        <p className="history-page__item-preview">{conversation.preview}</p>
                      ) : null}
                      <time className="history-page__item-time" dateTime={conversation.timestamp}>
                        {formatShanghaiDateTime(conversation.timestamp)}
                      </time>
                    </div>
                    {conversation.active ? (
                      <span className="history-page__item-badge">{copy.active}</span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <nav aria-label="history-pagination" className="history-page__pagination">
            <span>Page {currentPage + 1} / {totalPages}</span>
            <div>
              <button
                disabled={currentPage === 0}
                onClick={() => updatePage((value) => Math.max(0, value - 1))}
                type="button"
              >
                {copy.previousPage}
              </button>
              <button
                disabled={currentPage >= totalPages - 1}
                onClick={() => updatePage((value) => Math.min(totalPages - 1, value + 1))}
                type="button"
              >
                {copy.nextPage}
              </button>
            </div>
          </nav>
        </div>
      )}
    </section>
  );
}

export function HistoryRoutePage({ gateway }: { readonly gateway: ApiGateway }) {
  const { projectId } = useParams();
  if (!projectId) return <p role="alert">{copy.historyRouteInvalid}</p>;
  return <HistoryPage gateway={gateway} projectId={projectId} />;
}

export interface HistorySessionPageProps {
  readonly gateway: ApiGateway;
  readonly projectId: string;
  readonly sessionId: string;
}

/** One stored conversation, read only: works for a project the server is not bound to. */
export function HistorySessionPage({ gateway, projectId, sessionId }: HistorySessionPageProps) {
  const api = useMemo(() => createConversationApi(gateway), [gateway]);
  const session = useQuery({
    queryFn: () => api.sessionMessages(projectId, sessionId, "main"),
    queryKey: ["conversations", projectId, "main", "session", sessionId],
  });
  return (
    <section className="history-page history-page--session">
      <header className="history-page__header">
        <div>
          <p className="history-page__eyebrow">PROJECT / {projectId}</p>
          <h1>{copy.title}</h1>
        </div>
        <Link to={`/projects/${encodeURIComponent(projectId)}/history`}>{copy.backToHistory}</Link>
      </header>
      {session.isPending ? <p role="status">{copy.loadingSession}</p> : session.isError ? (
        <div>
          <p role="alert">{copy.sessionLoadFailed}</p>
          <button onClick={() => void session.refetch()} type="button">{copy.reload}</button>
        </div>
      ) : (
        <MessageList
          agentId="main"
          api={api}
          messages={session.data.messages}
          projectId={projectId}
          readOnly
        />
      )}
    </section>
  );
}

export function HistorySessionRoutePage({ gateway }: { readonly gateway: ApiGateway }) {
  const { projectId, sessionId } = useParams();
  if (!projectId || !sessionId) return <p role="alert">{copy.historyRouteInvalid}</p>;
  return <HistorySessionPage gateway={gateway} projectId={projectId} sessionId={sessionId} />;
}
