import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ConversationApi } from "../conversations/conversation-api";
import { MessageList } from "./MessageList";

function fakeApi(overrides: Partial<ConversationApi> = {}): ConversationApi {
  return {
    activate: async () => { throw new Error("unused"); },
    clear: async () => ({ activeSessionId: "s1" }),
    create: async () => { throw new Error("unused"); },
    delete: async () => ({ activeSessionId: "s1" }),
    editResend: async () => ({ commandId: "cmd", status: "accepted" }),
    interrupt: async () => ({ commandId: "cmd", status: "accepted" }),
    list: async () => ({ activeSessionId: "s1", conversations: [] }),
    messages: async () => ({ messages: [], sessionId: "s1" }),
    rename: async () => { throw new Error("unused"); },
    rollback: async () => ({ commandId: "cmd", conversationHistory: [], restoredFiles: 0, status: "accepted" }),
    sendFileContext: async () => ({ commandId: "cmd", status: "accepted" }),
    sendMessage: async () => ({ commandId: "cmd", status: "accepted" }),
    ...overrides,
  };
}

describe("MessageList", () => {
  it("keeps later history visible until edit-resend is confirmed by the server", async () => {
    let accept!: () => void;
    const editResend = vi.fn(() => new Promise<{ commandId: string; status: "accepted" }>((resolve) => {
      accept = () => resolve({ commandId: "edit", status: "accepted" });
    }));
    const messages = vi.fn().mockResolvedValue({
      messages: [{ content: "edited", message_id: "m2", role: "user" }],
      sessionId: "s1",
    });
    const user = userEvent.setup();
    render(<MessageList agentId="main" api={fakeApi({ editResend, messages })}
      messages={[
        { content: "hello", message_id: "m1", role: "user" },
        { content: "answer", message_id: "m1", role: "agent" },
      ]}
      requestEdit={() => "edited"}
    />);

    await user.click(screen.getByRole("button", { name: "编辑并重发 hello" }));
    expect(screen.getByText("answer")).toBeVisible();
    accept();
    expect(await screen.findByText("edited")).toBeVisible();
    await waitFor(() => expect(screen.queryByText("answer")).not.toBeInTheDocument());
    expect(editResend).toHaveBeenCalledWith("main", "m1", "edited", expect.any(String), expect.any(String));
  });

  it("shows restored file count and replaces history after rollback", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const rollback = vi.fn().mockResolvedValue({
      commandId: "rollback",
      conversationHistory: [{ content: "restored", message_id: "m0", role: "user" }],
      restoredFiles: 2,
      status: "accepted",
    });
    const user = userEvent.setup();
    render(<MessageList agentId="main" api={fakeApi({ rollback })}
      messages={[
        { checkpoint_id: "cp-1", content: "before change", message_id: "m1", role: "checkpoint" },
        { content: "unsafe <img src=x onerror=alert(1)>", role: "agent" },
      ]}
    />);

    expect(screen.getByText("unsafe <img src=x onerror=alert(1)>")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "回滚 before change" }));
    expect(await screen.findByText("restored")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("已恢复 2 个文件");
    expect(rollback).toHaveBeenCalledWith("main", "cp-1", expect.any(String), "m1");
  });

  it("refreshes authoritative history and reuses the mutation key after an ambiguous edit failure", async () => {
    const editResend = vi.fn()
      .mockRejectedValueOnce(new TypeError("network response lost"))
      .mockResolvedValueOnce({ commandId: "edit", status: "accepted" });
    const messages = vi.fn().mockResolvedValue({
      messages: [{ content: "server truth", message_id: "m1", role: "user" }],
      sessionId: "s1",
    });
    const user = userEvent.setup();
    render(<MessageList agentId="main" api={fakeApi({ editResend, messages })}
      messages={[{ content: "old", message_id: "m1", role: "user" }]}
      requestEdit={() => "edited"}
    />);

    await user.click(screen.getByRole("button", { name: "编辑并重发 old" }));
    expect(await screen.findByText("server truth")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("结果未知");
    await user.click(screen.getByRole("button", { name: "重试编辑重发" }));

    await waitFor(() => expect(editResend).toHaveBeenCalledTimes(2));
    expect(editResend.mock.calls[0]?.[3]).toBe(editResend.mock.calls[1]?.[3]);
    expect(editResend.mock.calls[0]?.[4]).toBe(editResend.mock.calls[1]?.[4]);
  });
});
