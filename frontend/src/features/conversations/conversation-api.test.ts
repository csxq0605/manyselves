import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createConversationApi } from "./conversation-api";

describe("ConversationApi", () => {
  it("requires the control lease for session mutations", async () => {
    const requestJson = vi.fn().mockResolvedValue({ activeSessionId: "s1", conversations: [] });
    const requestVoid = vi.fn().mockResolvedValue(undefined);
    const api = createConversationApi({ requestJson, requestVoid } as unknown as ApiGateway);

    await api.activate("s2", "critic");
    await api.rename("s2", "Review", "critic");

    expect(requestJson).toHaveBeenNthCalledWith(
      1,
      "/api/v1/conversations/s2/activate?agentId=critic",
      { method: "POST", requireLease: true },
    );
    expect(requestJson).toHaveBeenNthCalledWith(
      2,
      "/api/v1/conversations/s2",
      { json: { agentId: "critic", name: "Review" }, method: "PATCH", requireLease: true },
    );
  });

  it("reuses the caller idempotency key for retryable agent commands", async () => {
    const requestJson = vi.fn().mockResolvedValue({ commandId: "cmd-1", status: "accepted" });
    const api = createConversationApi({ requestJson } as unknown as ApiGateway);

    await api.sendMessage("main", "hello", "stable-key", "message-1");
    await api.sendMessage("main", "hello", "stable-key", "message-1");

    expect(requestJson).toHaveBeenCalledTimes(2);
    expect(requestJson).toHaveBeenNthCalledWith(1, "/api/v1/agents/main/messages", {
      headers: { "Idempotency-Key": "stable-key" },
      json: { content: "hello", messageId: "message-1", source: "user" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson.mock.calls[1]).toEqual(requestJson.mock.calls[0]);
  });
});
