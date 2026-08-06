import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ConversationApi } from "../conversations/conversation-api";
import type { RuntimeMessageView } from "../agents/event-reducer";
import { MessageList } from "./MessageList";

function fakeApi(overrides: Partial<ConversationApi> = {}): ConversationApi {
  return {
    activate: async () => { throw new Error("unused"); },
    clear: async (projectId) => ({ activeSessionId: "s1", projectId }),
    create: async () => { throw new Error("unused"); },
    delete: async (projectId) => ({ activeSessionId: "s1", projectId }),
    editResend: async () => ({ commandId: "cmd", status: "accepted" }),
    interrupt: async () => ({ commandId: "cmd", status: "accepted" }),
    list: async (projectId) => ({ activeSessionId: "s1", conversations: [], projectId }),
    messages: async (projectId) => ({ messages: [], projectId, sessionId: "s1" }),
    rename: async () => { throw new Error("unused"); },
    rollback: async () => ({ commandId: "cmd", conversationHistory: [], restoredFiles: 0, status: "accepted" }),
    sendFileContext: async () => ({ commandId: "cmd", status: "accepted" }),
    sendMessage: async () => ({ commandId: "cmd", status: "accepted" }),
    ...overrides,
  };
}

describe("MessageList", () => {
  it("moves tool call records into a floating runtime panel instead of the chat stream", async () => {
    const user = userEvent.setup();
    render(
      <MessageList
        agentId="main"
        api={fakeApi()}
        messages={[
          { content: "hello", message_id: "m1", role: "user", timestamp: "2026-08-05T22:20:10" },
          { content: "read", role: "tool_call", timestamp: "2026-08-05T22:20:12" },
          { content: "read", role: "tool_result", timestamp: "2026-08-05T22:20:14" },
        ]}
        projectId="project-1"
      />,
    );

    const chatItems = screen.getByLabelText("消息历史").querySelectorAll("li.message");
    expect(chatItems).toHaveLength(1);
    expect(screen.getByRole("button", { name: "展开 Agent 运行态" })).toHaveTextContent("Agent 运行态2");

    await user.click(screen.getByRole("button", { name: "展开 Agent 运行态" }));
    expect(screen.getByRole("list", { name: "运行态记录" })).toHaveTextContent("tool_call");
    expect(screen.getByRole("list", { name: "运行态记录" })).toHaveTextContent("tool_result");
  });

  it("summarizes agent status and task progress inside the floating runtime panel", async () => {
    const user = userEvent.setup();
    render(
      <MessageList
        agentId="main"
        api={fakeApi()}
        messages={[{ content: "read", role: "tool_call", timestamp: "2026-08-05T22:20:12" }]}
        projectId="project-1"
        runtimeSummary={{
          activeTasks: 2,
          agentStatuses: { main: "running", researcher: "waiting" },
          completedTasks: 3,
          currentTasks: [
            { agentPath: "main -> researcher", brief: "Build report", status: "running" },
            { agentPath: "main -> reviewer", brief: "Review draft", status: "waiting" },
          ],
          ready: true,
          runningTools: 1,
          totalTasks: 5,
        }}
      />,
    );

    expect(screen.getByRole("button", { name: "\u5c55\u5f00 Agent \u8fd0\u884c\u6001" })).toHaveTextContent("2 \u8fdb\u884c\u4e2d");
    expect(screen.getByRole("button", { name: "\u5c55\u5f00 Agent \u8fd0\u884c\u6001" })).toHaveTextContent("3/5 \u5b8c\u6210");

    await user.click(screen.getByRole("button", { name: "\u5c55\u5f00 Agent \u8fd0\u884c\u6001" }));

    expect(screen.getByText("\u670d\u52a1\u5c31\u7eea")).toBeVisible();
    expect(screen.getByText("main")).toBeVisible();
    expect(screen.getByText("running")).toBeVisible();
    expect(screen.getByText("Build report")).toBeVisible();
    expect(screen.getByText("1 \u4e2a\u5de5\u5177\u8fd0\u884c\u4e2d")).toBeVisible();
  });

  it("scrolls to the newest message when history changes", () => {
    const scrollIntoView = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView;
    const { rerender } = render(
      <MessageList
        agentId="main"
        api={fakeApi()}
        messages={[{ content: "first", message_id: "m1", role: "user" }]}
        projectId="project-1"
      />,
    );

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "end" });
    scrollIntoView.mockClear();

    rerender(
      <MessageList
        agentId="main"
        api={fakeApi()}
        messages={[
          { content: "first", message_id: "m1", role: "user" },
          { content: "second", message_id: "m2", role: "agent" },
        ]}
        projectId="project-1"
      />,
    );

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "end" });
  });

  it("places user metadata above the bubble and keeps the re-edit action compact", () => {
    render(
      <MessageList
        agentId="main"
        api={fakeApi()}
        messages={[{ content: "你是上面模型", message_id: "m1", role: "user", timestamp: "2026-08-05T20:39:49" }]}
        projectId="project-1"
      />,
    );

    const message = screen.getByText("你是上面模型").closest("li");
    expect(message?.querySelector(".message__meta")?.textContent).toBe("2026-08-05T20:39:49 user");
    expect(screen.getByRole("button", { name: "重编辑并重发 你是上面模型" })).toHaveTextContent("✎ 重编辑");
  });

  it("keeps non-user message metadata as role followed by timestamp", () => {
    render(
      <MessageList
        agentId="main"
        api={fakeApi()}
        messages={[{ checkpoint_id: "cp-1", content: "checkpoint saved", message_id: "m1", role: "checkpoint", timestamp: "2026-08-05T20:55:31" }]}
        projectId="project-1"
      />,
    );

    const message = screen.getByText("checkpoint saved").closest("li");
    expect(message?.querySelector(".message__meta")?.textContent).toBe("checkpoint 2026-08-05T20:55:31");
  });

  it("attaches pre checkpoints to their target message instead of rendering a checkpoint row", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const rollback = vi.fn().mockResolvedValue({
      commandId: "rollback",
      conversationHistory: [{ content: "restored", message_id: "m0", role: "user" }],
      restoredFiles: 1,
      status: "accepted",
    });
    const user = userEvent.setup();
    render(
      <MessageList
        agentId="main"
        api={fakeApi({ rollback })}
        messages={[
          { content: "before edit", message_id: "m1", role: "user", timestamp: "2026-08-05T20:39:49" },
          { checkpoint_id: "cp-1", content: "pre:user", message_id: "m1", role: "checkpoint", timestamp: "2026-08-05T20:39:50" },
        ]}
        projectId="project-1"
      />,
    );

    expect(screen.queryByText("pre:user")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "回滚到此处：before edit" }));
    expect(await screen.findByText("restored")).toBeVisible();
    expect(rollback).toHaveBeenCalledWith("project-1", "main", "cp-1", expect.any(String), "m1");
  });

  it("shows live streaming messages and removes them when authoritative history contains the message ID", () => {
    const liveMessages: RuntimeMessageView[] = [{
      agentId: "main",
      content: "streaming answer",
      id: "message-live",
      sessionId: "session-1",
      status: "streaming",
      thinking: "",
      timestamp: "2026-08-03T08:00:00Z",
    }];
    const { rerender } = render(
      <MessageList
        agentId="main"
        api={fakeApi()}
        liveMessages={liveMessages}
        messages={[{ content: "question", message_id: "message-live", role: "user" }]}
        projectId="project-1"
      />,
    );

    expect(screen.getByText("streaming answer")).toBeVisible();
    expect(screen.getByText("流式生成中")).toBeVisible();

    rerender(
      <MessageList
        agentId="main"
        api={fakeApi()}
        liveMessages={liveMessages}
        messages={[
          { content: "question", message_id: "message-live", role: "user" },
          { content: "server answer", message_id: "message-live", role: "agent" },
        ]}
        projectId="project-1"
      />,
    );

    expect(screen.getByText("server answer")).toBeVisible();
    expect(screen.queryByText("streaming answer")).not.toBeInTheDocument();
  });

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
      projectId="project-1"
      requestEdit={() => "edited"}
    />);

    await user.click(screen.getByRole("button", { name: "重编辑并重发 hello" }));
    expect(screen.getByText("answer")).toBeVisible();
    accept();
    expect(await screen.findByText("edited")).toBeVisible();
    await waitFor(() => expect(screen.queryByText("answer")).not.toBeInTheDocument());
    expect(editResend).toHaveBeenCalledWith("project-1", "main", "m1", "edited", expect.any(String), expect.any(String));
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
      projectId="project-1"
    />);

    expect(screen.getByText("unsafe <img src=x onerror=alert(1)>")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "回滚 before change" }));
    expect(await screen.findByText("restored")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("已恢复 2 个文件");
    expect(rollback).toHaveBeenCalledWith("project-1", "main", "cp-1", expect.any(String), "m1");
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
      projectId="project-1"
      requestEdit={() => "edited"}
    />);

    await user.click(screen.getByRole("button", { name: "重编辑并重发 old" }));
    expect(await screen.findByText("server truth")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("结果未知");
    await user.click(screen.getByRole("button", { name: "重试编辑重发" }));

    await waitFor(() => expect(editResend).toHaveBeenCalledTimes(2));
    expect(editResend.mock.calls[0]?.[4]).toBe(editResend.mock.calls[1]?.[4]);
    expect(editResend.mock.calls[0]?.[5]).toBe(editResend.mock.calls[1]?.[5]);
  });
});
