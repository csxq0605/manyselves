import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ConversationApi } from "../conversations/conversation-api";
import { MessageComposer } from "./MessageComposer";
import { createConversationMessageStore } from "./message-store";

function fakeApi(overrides: Partial<ConversationApi> = {}): ConversationApi {
  return {
    activate: async () => { throw new Error("unused"); },
    clear: async () => ({ activeSessionId: "s2" }),
    create: async (name) => ({ active: true, name, preview: "", sessionId: "s2", timestamp: "now" }),
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

describe("MessageComposer", () => {
  it("preserves drafts per session", async () => {
    const store = createConversationMessageStore();
    const user = userEvent.setup();
    const view = render(<MessageComposer agentId="main" api={fakeApi()} sessionId="s1" store={store} />);

    await user.type(screen.getByRole("textbox", { name: "消息" }), "first");
    view.rerender(<MessageComposer agentId="main" api={fakeApi()} sessionId="s2" store={store} />);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("");
    await user.type(screen.getByRole("textbox", { name: "消息" }), "second");
    view.rerender(<MessageComposer agentId="main" api={fakeApi()} sessionId="s1" store={store} />);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("first");
  });

  it("reuses the same idempotency key when retrying one failed send", async () => {
    const sendMessage = vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ commandId: "cmd", status: "accepted" });
    const store = createConversationMessageStore();
    const user = userEvent.setup();
    render(<MessageComposer agentId="main" api={fakeApi({ sendMessage })} sessionId="s1" store={store} />);

    await user.type(screen.getByRole("textbox", { name: "消息" }), "hello");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("发送失败");
    await user.click(screen.getByRole("button", { name: "重试发送" }));

    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(2));
    expect(sendMessage.mock.calls[0]?.[2]).toBe(sendMessage.mock.calls[1]?.[2]);
    expect(sendMessage.mock.calls[0]?.[3]).toBe(sendMessage.mock.calls[1]?.[3]);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("");
  });

  it("sends server file, current editor, and current selection context before the message", async () => {
    const sendFileContext = vi.fn().mockResolvedValue({ commandId: "context", status: "accepted" });
    const sendMessage = vi.fn().mockResolvedValue({ commandId: "message", status: "accepted" });
    const user = userEvent.setup();
    render(<MessageComposer
      agentId="main"
      api={fakeApi({ sendFileContext, sendMessage })}
      availableFiles={["Inputs/brief.md"]}
      currentEditorPath="src/main.py"
      currentSelection={{ endLine: 8, path: "src/main.py", startLine: 3 }}
      sessionId="s1"
      store={createConversationMessageStore()}
    />);

    await user.selectOptions(screen.getByRole("combobox", { name: "服务器文件" }), "Inputs/brief.md");
    await user.click(screen.getByRole("button", { name: "添加服务器文件" }));
    await user.click(screen.getByRole("checkbox", { name: "引用当前编辑文件" }));
    await user.click(screen.getByRole("checkbox", { name: "引用当前选区" }));
    await user.type(screen.getByRole("textbox", { name: "消息" }), "review");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(sendMessage).toHaveBeenCalledOnce());
    expect(sendFileContext).toHaveBeenCalledTimes(3);
    expect(sendFileContext.mock.calls.map((call) => call[1])).toEqual([
      { file: "Inputs/brief.md", type: "file" },
      { file: "src/main.py", type: "file" },
      { endLine: 8, file: "src/main.py", startLine: 3, type: "selection" },
    ]);
  });

  it("executes slash history and stop without sending them as messages", async () => {
    const interrupt = vi.fn().mockResolvedValue({ commandId: "stop", status: "accepted" });
    const sendMessage = vi.fn();
    const onHistoryRequested = vi.fn();
    const user = userEvent.setup();
    render(<MessageComposer agentId="main" api={fakeApi({ interrupt, sendMessage })}
      onHistoryRequested={onHistoryRequested} sessionId="s1" store={createConversationMessageStore()} />);

    await user.type(screen.getByRole("textbox", { name: "消息" }), "/history");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(onHistoryRequested).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "停止生成" }));
    await waitFor(() => expect(interrupt).toHaveBeenCalledOnce());
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("blocks duplicate asynchronous slash commands", async () => {
    let finishCreate!: () => void;
    const create = vi.fn(() => new Promise<Awaited<ReturnType<ConversationApi["create"]>>>((resolve) => {
      finishCreate = () => resolve({
        active: true, name: "新会话", preview: "", sessionId: "s2", timestamp: "now",
      });
    }));
    const user = userEvent.setup();
    render(<MessageComposer agentId="main" api={fakeApi({ create })}
      sessionId="s1" store={createConversationMessageStore()} />);

    await user.type(screen.getByRole("textbox", { name: "消息" }), "/new");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "消息" }), { key: "Enter" });
    expect(create).toHaveBeenCalledOnce();
    finishCreate();
  });

  it("drops selected contexts and failed retries when remounted for another session", async () => {
    const sendMessage = vi.fn().mockRejectedValue(new Error("offline"));
    const store = createConversationMessageStore();
    const user = userEvent.setup();
    const view = render(<MessageComposer agentId="main" api={fakeApi({ sendMessage })}
      availableFiles={["Inputs/one.md"]} key="s1" sessionId="s1" store={store} />);

    await user.click(screen.getByRole("button", { name: "添加服务器文件" }));
    await user.type(screen.getByRole("textbox", { name: "消息" }), "first");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("button", { name: "重试发送" })).toBeVisible();

    view.rerender(<MessageComposer agentId="main" api={fakeApi({ sendMessage })}
      availableFiles={["Inputs/two.md"]} key="s2" sessionId="s2" store={store} />);
    expect(screen.queryByRole("button", { name: "重试发送" })).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "已选引用" })).not.toBeInTheDocument();
  });
});
