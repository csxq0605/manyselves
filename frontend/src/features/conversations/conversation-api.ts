import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

type AcceptedCommand = components["schemas"]["AcceptedCommandResponse"];
type FileContextRequest = components["schemas"]["FileContextRequest"];
type RollbackResponse = components["schemas"]["RollbackResponse"];
type GeneratedConversationSummary = components["schemas"]["ConversationResponse"];
type GeneratedConversationListSnapshot = components["schemas"]["ConversationListResponse"];
type GeneratedConversationMessagesSnapshot = components["schemas"]["ConversationMessagesResponse"];

export type ConversationSummary = GeneratedConversationSummary & { readonly projectId: string };
export type ConversationListSnapshot = Omit<GeneratedConversationListSnapshot, "conversations"> & {
  readonly conversations: readonly ConversationSummary[];
  readonly projectId: string;
};
export type ConversationMessagesSnapshot = GeneratedConversationMessagesSnapshot & {
  readonly projectId: string;
};

export interface ActiveSessionSnapshot {
  readonly activeSessionId: string;
  readonly projectId: string;
}

export interface ConversationApi {
  activate(projectId: string, sessionId: string, agentId: string): Promise<ConversationSummary>;
  clear(projectId: string, agentId: string): Promise<ActiveSessionSnapshot>;
  create(projectId: string, name: string, agentId: string): Promise<ConversationSummary>;
  delete(projectId: string, sessionId: string, agentId: string): Promise<ActiveSessionSnapshot>;
  editResend(
    projectId: string,
    agentId: string,
    targetMessageId: string,
    content: string,
    idempotencyKey: string,
    messageId?: string,
  ): Promise<AcceptedCommand>;
  interrupt(agentId: string, idempotencyKey: string): Promise<AcceptedCommand>;
  list(projectId: string, agentId: string): Promise<ConversationListSnapshot>;
  messages(projectId: string, agentId: string): Promise<ConversationMessagesSnapshot>;
  rename(projectId: string, sessionId: string, name: string, agentId: string): Promise<ConversationSummary>;
  rollback(
    projectId: string,
    agentId: string,
    checkpointId: string,
    idempotencyKey: string,
    targetMessageId?: string,
  ): Promise<RollbackResponse>;
  sendFileContext(
    projectId: string,
    agentId: string,
    context: FileContextRequest,
    idempotencyKey: string,
  ): Promise<AcceptedCommand>;
  sendMessage(
    projectId: string,
    agentId: string,
    content: string,
    idempotencyKey: string,
    messageId?: string,
  ): Promise<AcceptedCommand>;
}

function projectAgentQuery(projectId: string, agentId: string): string {
  return new URLSearchParams({ projectId, agentId }).toString();
}

function commandOptions(idempotencyKey: string, json?: unknown) {
  return {
    headers: { "Idempotency-Key": idempotencyKey },
    ...(json === undefined ? {} : { json }),
    method: "POST" as const,
    requireLease: true,
  };
}

export function createConversationApi(gateway: ApiGateway): ConversationApi {
  return {
    activate: (projectId, sessionId, agentId) => gateway.requestJson<ConversationSummary>(
      `/api/v1/conversations/${encodeURIComponent(sessionId)}/activate?${projectAgentQuery(projectId, agentId)}`,
      { method: "POST", requireLease: true },
    ),
    clear: (projectId, agentId) => gateway.requestJson<ActiveSessionSnapshot>(
      `/api/v1/conversations/clear?${projectAgentQuery(projectId, agentId)}`,
      { method: "POST", requireLease: true },
    ),
    create: (projectId, name, agentId) => gateway.requestJson<ConversationSummary>(
      "/api/v1/conversations",
      { json: { projectId, agentId, name }, method: "POST", requireLease: true },
    ),
    delete: (projectId, sessionId, agentId) => gateway.requestJson<ActiveSessionSnapshot>(
      `/api/v1/conversations/${encodeURIComponent(sessionId)}?${projectAgentQuery(projectId, agentId)}`,
      { method: "DELETE", requireLease: true },
    ),
    editResend: (projectId, agentId, targetMessageId, content, idempotencyKey, messageId) =>
      gateway.requestJson<AcceptedCommand>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/messages/${encodeURIComponent(targetMessageId)}/edit-resend`,
        commandOptions(idempotencyKey, {
          projectId,
          content,
          ...(messageId === undefined ? {} : { messageId }),
        }),
      ),
    interrupt: (agentId, idempotencyKey) => gateway.requestJson<AcceptedCommand>(
      `/api/v1/agents/${encodeURIComponent(agentId)}/interrupt`,
      commandOptions(idempotencyKey),
    ),
    list: (projectId, agentId) => gateway.requestJson<ConversationListSnapshot>(
      `/api/v1/conversations?${projectAgentQuery(projectId, agentId)}`,
    ),
    messages: (projectId, agentId) => gateway.requestJson<ConversationMessagesSnapshot>(
      `/api/v1/conversations/messages?${projectAgentQuery(projectId, agentId)}`,
    ),
    rename: (projectId, sessionId, name, agentId) => gateway.requestJson<ConversationSummary>(
      `/api/v1/conversations/${encodeURIComponent(sessionId)}`,
      { json: { projectId, agentId, name }, method: "PATCH", requireLease: true },
    ),
    rollback: (projectId, agentId, checkpointId, idempotencyKey, targetMessageId) =>
      gateway.requestJson<RollbackResponse>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/rollback`,
        commandOptions(idempotencyKey, {
          projectId,
          checkpointId,
          ...(targetMessageId === undefined ? {} : { targetMessageId }),
        }),
      ),
    sendFileContext: (projectId, agentId, context, idempotencyKey) =>
      gateway.requestJson<AcceptedCommand>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/file-context`,
        commandOptions(idempotencyKey, { projectId, ...context }),
      ),
    sendMessage: (projectId, agentId, content, idempotencyKey, messageId) =>
      gateway.requestJson<AcceptedCommand>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/messages`,
        commandOptions(idempotencyKey, {
          projectId,
          content,
          ...(messageId === undefined ? {} : { messageId }),
          source: "user",
        }),
      ),
  };
}
