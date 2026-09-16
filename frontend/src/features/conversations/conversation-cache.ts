import type { QueryClient } from "@tanstack/react-query";

import type { ConversationListSnapshot, ConversationSummary } from "./conversation-api";

export function conversationListQueryKey(projectId: string, agentId: string) {
  return ["conversations", projectId, agentId] as const;
}

export async function cacheCreatedConversation(
  queryClient: QueryClient,
  projectId: string,
  agentId: string,
  created: ConversationSummary,
): Promise<void> {
  const queryKey = conversationListQueryKey(projectId, agentId);

  // Stop an older list request before publishing the create response. Otherwise
  // that request can resolve later and replace the newly created conversation.
  await queryClient.cancelQueries({ exact: true, queryKey });
  queryClient.setQueryData<ConversationListSnapshot>(queryKey, (current) => ({
    activeSessionId: created.sessionId,
    conversations: [
      { ...created, active: true },
      ...(current?.projectId === projectId
        ? current.conversations
          .filter((item) => item.sessionId !== created.sessionId)
          .map((item) => ({ ...item, active: false }))
        : []),
    ],
    projectId,
  }));
  await queryClient.invalidateQueries({ exact: true, queryKey, refetchType: "none" });
}
