import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

type AcceptedCommand = components["schemas"]["AcceptedCommandResponse"];
type FileContextRequest = components["schemas"]["FileContextRequest"];
type RollbackResponse = components["schemas"]["RollbackResponse"];

export type ConversationSummary = components["schemas"]["ConversationResponse"];
export type ConversationListSnapshot = components["schemas"]["ConversationListResponse"];
export type ConversationMessagesSnapshot = components["schemas"]["ConversationMessagesResponse"];

export interface ActiveSessionSnapshot {
  readonly activeSessionId: string;
}

export interface ConversationApi {
  activate(sessionId: string, agentId: string): Promise<ConversationSummary>;
  clear(agentId: string): Promise<ActiveSessionSnapshot>;
  create(name: string, agentId: string): Promise<ConversationSummary>;
  delete(sessionId: string, agentId: string): Promise<ActiveSessionSnapshot>;
  editResend(
    agentId: string,
    targetMessageId: string,
    content: string,
    idempotencyKey: string,
    messageId?: string,
  ): Promise<AcceptedCommand>;
  interrupt(agentId: string, idempotencyKey: string): Promise<AcceptedCommand>;
  list(agentId: string): Promise<ConversationListSnapshot>;
  messages(agentId: string): Promise<ConversationMessagesSnapshot>;
  rename(sessionId: string, name: string, agentId: string): Promise<ConversationSummary>;
  rollback(
    agentId: string,
    checkpointId: string,
    idempotencyKey: string,
    targetMessageId?: string,
  ): Promise<RollbackResponse>;
  sendFileContext(
    agentId: string,
    context: FileContextRequest,
    idempotencyKey: string,
  ): Promise<AcceptedCommand>;
  sendMessage(
    agentId: string,
    content: string,
    idempotencyKey: string,
    messageId?: string,
  ): Promise<AcceptedCommand>;
}

function agentQuery(agentId: string): string {
  return new URLSearchParams({ agentId }).toString();
}

function commandOptions(
  idempotencyKey: string,
  json?: unknown,
) {
  return {
    headers: { "Idempotency-Key": idempotencyKey },
    ...(json === undefined ? {} : { json }),
    method: "POST" as const,
    requireLease: true,
  };
}

export function createConversationApi(gateway: ApiGateway): ConversationApi {
  return {
    activate: (sessionId, agentId) => gateway.requestJson<ConversationSummary>(
      `/api/v1/conversations/${encodeURIComponent(sessionId)}/activate?${agentQuery(agentId)}`,
      { method: "POST", requireLease: true },
    ),
    clear: (agentId) => gateway.requestJson<ActiveSessionSnapshot>(
      `/api/v1/conversations/clear?${agentQuery(agentId)}`,
      { method: "POST", requireLease: true },
    ),
    create: (name, agentId) => gateway.requestJson<ConversationSummary>(
      "/api/v1/conversations",
      { json: { agentId, name }, method: "POST", requireLease: true },
    ),
    delete: (sessionId, agentId) => gateway.requestJson<ActiveSessionSnapshot>(
      `/api/v1/conversations/${encodeURIComponent(sessionId)}?${agentQuery(agentId)}`,
      { method: "DELETE", requireLease: true },
    ),
    editResend: (agentId, targetMessageId, content, idempotencyKey, messageId) =>
      gateway.requestJson<AcceptedCommand>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/messages/${encodeURIComponent(targetMessageId)}/edit-resend`,
        commandOptions(idempotencyKey, {
          content,
          ...(messageId === undefined ? {} : { messageId }),
        }),
      ),
    interrupt: (agentId, idempotencyKey) => gateway.requestJson<AcceptedCommand>(
      `/api/v1/agents/${encodeURIComponent(agentId)}/interrupt`,
      commandOptions(idempotencyKey),
    ),
    list: (agentId) => gateway.requestJson<ConversationListSnapshot>(
      `/api/v1/conversations?${agentQuery(agentId)}`,
    ),
    messages: (agentId) => gateway.requestJson<ConversationMessagesSnapshot>(
      `/api/v1/conversations/messages?${agentQuery(agentId)}`,
    ),
    rename: (sessionId, name, agentId) => gateway.requestJson<ConversationSummary>(
      `/api/v1/conversations/${encodeURIComponent(sessionId)}`,
      { json: { agentId, name }, method: "PATCH", requireLease: true },
    ),
    rollback: (agentId, checkpointId, idempotencyKey, targetMessageId) =>
      gateway.requestJson<RollbackResponse>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/rollback`,
        commandOptions(idempotencyKey, {
          checkpointId,
          ...(targetMessageId === undefined ? {} : { targetMessageId }),
        }),
      ),
    sendFileContext: (agentId, context, idempotencyKey) =>
      gateway.requestJson<AcceptedCommand>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/file-context`,
        commandOptions(idempotencyKey, context),
      ),
    sendMessage: (agentId, content, idempotencyKey, messageId) =>
      gateway.requestJson<AcceptedCommand>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/messages`,
        commandOptions(idempotencyKey, {
          content,
          ...(messageId === undefined ? {} : { messageId }),
          source: "user",
        }),
      ),
  };
}
