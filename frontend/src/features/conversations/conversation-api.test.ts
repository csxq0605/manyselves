import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createConversationApi } from "./conversation-api";

describe("ConversationApi project identity", () => {
  it("sends projectId on list, messages, activation, clear, and delete queries", async () => {
    const requestJson = vi.fn().mockResolvedValue({ activeSessionId: "s1", conversations: [], projectId: "energy" });
    const api = createConversationApi({ requestJson } as unknown as ApiGateway);

    await api.list("energy", "critic");
    await api.messages("energy", "critic");
    await api.activate("energy", "s2", "critic");
    await api.clear("energy", "critic");
    await api.delete("energy", "s2", "critic");

    expect(requestJson.mock.calls).toEqual([
      ["/api/v1/conversations?projectId=energy&agentId=critic"],
      ["/api/v1/conversations/messages?projectId=energy&agentId=critic"],
      ["/api/v1/conversations/s2/activate?projectId=energy&agentId=critic", { method: "POST", requireLease: true }],
      ["/api/v1/conversations/clear?projectId=energy&agentId=critic", { method: "POST", requireLease: true }],
      ["/api/v1/conversations/s2?projectId=energy&agentId=critic", { method: "DELETE", requireLease: true }],
    ]);
  });

  it("sends projectId in create and rename JSON while preserving the lease", async () => {
    const requestJson = vi.fn().mockResolvedValue({
      active: true, name: "Review", preview: "", projectId: "energy", sessionId: "s2", timestamp: "now",
    });
    const api = createConversationApi({ requestJson } as unknown as ApiGateway);

    await api.create("energy", "Review", "critic");
    await api.rename("energy", "s2", "Renamed", "critic");

    expect(requestJson).toHaveBeenNthCalledWith(1, "/api/v1/conversations", {
      json: { agentId: "critic", name: "Review", projectId: "energy" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(2, "/api/v1/conversations/s2", {
      json: { agentId: "critic", name: "Renamed", projectId: "energy" },
      method: "PATCH",
      requireLease: true,
    });
  });

  it("adds projectId only to conversation command bodies and keeps interrupt unchanged", async () => {
    const requestJson = vi.fn().mockResolvedValue({ commandId: "cmd-1", status: "accepted" });
    const api = createConversationApi({ requestJson } as unknown as ApiGateway);

    await api.sendMessage("energy", "main", "hello", "stable-key", "message-1");
    await api.sendFileContext("energy", "main", { file: "Inputs/brief.txt", type: "file" }, "context-key");
    await api.editResend("energy", "main", "message-1", "edited", "edit-key", "message-2");
    await api.rollback("energy", "main", "checkpoint-1", "rollback-key", "message-1");
    await api.interrupt("main", "interrupt-key");

    expect(requestJson).toHaveBeenNthCalledWith(1, "/api/v1/agents/main/messages", {
      headers: { "Idempotency-Key": "stable-key" },
      json: { content: "hello", messageId: "message-1", projectId: "energy", source: "user" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(2, "/api/v1/agents/main/file-context", {
      headers: { "Idempotency-Key": "context-key" },
      json: { file: "Inputs/brief.txt", projectId: "energy", type: "file" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(3, "/api/v1/agents/main/messages/message-1/edit-resend", {
      headers: { "Idempotency-Key": "edit-key" },
      json: { content: "edited", messageId: "message-2", projectId: "energy" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(4, "/api/v1/agents/main/rollback", {
      headers: { "Idempotency-Key": "rollback-key" },
      json: { checkpointId: "checkpoint-1", projectId: "energy", targetMessageId: "message-1" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(5, "/api/v1/agents/main/interrupt", {
      headers: { "Idempotency-Key": "interrupt-key" },
      method: "POST",
      requireLease: true,
    });
  });
});
