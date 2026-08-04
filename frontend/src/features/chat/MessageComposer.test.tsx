import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/gateway";
import type { FileApi, FileEntry } from "../files/file-api";
import type { ConversationApi } from "../conversations/conversation-api";
import { MessageComposer } from "./MessageComposer";
import { createConversationMessageStore } from "./message-store";

const uploadedEntry: FileEntry = {
  kind: "file",
  modifiedAt: "2026-08-04T00:00:00Z",
  name: "brief.txt",
  path: "Inputs/brief.txt",
  revision: "a".repeat(64),
  size: 5,
};

function fakeFileApi(overrides: Partial<FileApi> = {}): FileApi {
  return {
    createEntry: async () => uploadedEntry,
    deleteEntry: async () => undefined,
    download: async () => new Blob(),
    listTree: async () => [],
    renameEntry: async () => uploadedEntry,
    upload: async () => uploadedEntry,
    ...overrides,
  };
}

function fakeApi(overrides: Partial<ConversationApi> = {}): ConversationApi {
  return {
    activate: async () => { throw new Error("unused"); },
    clear: async () => ({ activeSessionId: "s2", projectId: "project-1" }),
    create: async (projectId, name) => ({ active: true, name, preview: "", projectId, sessionId: "s2", timestamp: "now" }),
    delete: async () => ({ activeSessionId: "s1", projectId: "project-1" }),
    editResend: async () => ({ commandId: "cmd", status: "accepted" }),
    interrupt: async () => ({ commandId: "cmd", status: "accepted" }),
    list: async (projectId) => ({ activeSessionId: "s1", conversations: [], projectId }),
    messages: async (projectId) => ({ messages: [], projectId, sessionId: "s1" }),
    rename: async () => { throw new Error("unused"); },
    rollback: async () => ({ commandId: "cmd", conversationHistory: [], restoredFiles: 0, status: "accepted" }),
    sendFileContext: async () => ({ commandId: "cmd", status: "accepted" }),
    sendMessage: async () => ({ commandId: "cmd", status: "accepted" }),
    ...overrides,
  } as ConversationApi;
}

function renderComposer(options: {
  readonly api?: ConversationApi;
  readonly fileApi?: FileApi;
  readonly onHistoryRequested?: () => void;
  readonly projectId?: string;
  readonly sessionId?: string;
} = {}) {
  const store = createConversationMessageStore();
  const props = {
    agentId: "main",
    api: options.api ?? fakeApi(),
    fileApi: options.fileApi ?? fakeFileApi(),
    ...(options.onHistoryRequested ? { onHistoryRequested: options.onHistoryRequested } : {}),
    projectId: options.projectId ?? "project-1",
    sessionId: options.sessionId ?? "s1",
    store,
  };
  return { props, store, view: render(<MessageComposer {...props} />) };
}

describe("MessageComposer", () => {
  it("presents a concise browser-upload composer", () => {
    renderComposer();

    expect(screen.getByRole("textbox", { name: "消息" })).toHaveAttribute(
      "placeholder",
      "输入消息，Enter 发送，Shift+Enter 换行",
    );
    expect(screen.getByRole("button", { name: "上传本地文件" })).toHaveAttribute(
      "title",
      "上传本地文件",
    );
    expect(screen.getByText("文件将保存到项目“输入”")).toBeVisible();
    expect(screen.getByRole("button", { name: "发送" })).toHaveTextContent("↑");
    expect(screen.queryByRole("button", { name: "停止生成" })).not.toBeInTheDocument();
  });

  it("preserves drafts per project session without leaking same-named sessions", async () => {
    const { props, store, view } = renderComposer();
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox", { name: "消息" }), "first");
    view.rerender(<MessageComposer {...props} sessionId="s2" />);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("");
    await user.type(screen.getByRole("textbox", { name: "消息" }), "second");
    view.rerender(<MessageComposer {...props} sessionId="s1" />);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("first");

    view.rerender(<MessageComposer {...props} projectId="project-2" sessionId="s1" store={store} />);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("");
  });

  it("uploads browser File objects to the current project Inputs and shows successful chips", async () => {
    const upload = vi.fn().mockResolvedValue(uploadedEntry);
    const user = userEvent.setup();
    renderComposer({ fileApi: fakeFileApi({ upload }), projectId: "energy" });
    const file = new File(["brief"], "brief.txt", { type: "text/plain" });

    await user.click(screen.getByRole("button", { name: "上传本地文件" }));
    const input = screen.getByLabelText("选择本地文件");
    await user.upload(input, file);

    await waitFor(() => expect(upload).toHaveBeenCalledWith(
      "energy",
      "Inputs/brief.txt",
      file,
      "reject",
      undefined,
      expect.any(AbortSignal),
    ));
    expect(input).toHaveAttribute("multiple");
    expect(await screen.findByRole("list", { name: "待发送附件" })).toHaveTextContent("brief.txt");
    expect(screen.getByRole("list", { name: "待发送附件" })).toHaveTextContent("5 B");
  });

  it("keeps failed uploads out of chips and surfaces duplicate conflicts", async () => {
    const upload = vi.fn().mockRejectedValue(new ApiError({
      code: "FILE_ALREADY_EXISTS",
      details: { path: "Inputs/brief.txt" },
      message: "already exists",
      requestId: "request-1",
      retryable: false,
      status: 409,
    }));
    const user = userEvent.setup();
    renderComposer({ fileApi: fakeFileApi({ upload }) });

    await user.upload(screen.getByLabelText("选择本地文件"), new File(["x"], "brief.txt"));

    expect(await screen.findByRole("alert")).toHaveTextContent("同名文件已存在");
    expect(screen.queryByRole("list", { name: "待发送附件" })).not.toBeInTheDocument();
  });

  it("does not send a message before selected files finish uploading", async () => {
    let finishUpload!: () => void;
    const upload = vi.fn(() => new Promise<FileEntry>((resolve) => {
      finishUpload = () => resolve(uploadedEntry);
    }));
    const sendMessage = vi.fn().mockResolvedValue({ commandId: "message", status: "accepted" });
    const user = userEvent.setup();
    renderComposer({ api: fakeApi({ sendMessage }), fileApi: fakeFileApi({ upload }) });

    await user.upload(screen.getByLabelText("选择本地文件"), new File(["brief"], "brief.txt"));
    await user.type(screen.getByRole("textbox", { name: "消息" }), "review");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    finishUpload();
    expect(await screen.findByRole("list", { name: "待发送附件" })).toBeVisible();
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("removes a pending chip without deleting the uploaded server file", async () => {
    const deleteEntry = vi.fn();
    const user = userEvent.setup();
    renderComposer({ fileApi: fakeFileApi({ deleteEntry }) });

    await user.upload(screen.getByLabelText("选择本地文件"), new File(["brief"], "brief.txt"));
    await user.click(await screen.findByRole("button", { name: "移除 brief.txt" }));

    expect(screen.queryByRole("list", { name: "待发送附件" })).not.toBeInTheDocument();
    expect(deleteEntry).not.toHaveBeenCalled();
  });

  it("sends only logical uploaded references and strips browser-local path segments", async () => {
    const upload = vi.fn().mockResolvedValue(uploadedEntry);
    const sendFileContext = vi.fn().mockResolvedValue({ commandId: "context", status: "accepted" });
    const sendMessage = vi.fn().mockResolvedValue({ commandId: "message", status: "accepted" });
    const user = userEvent.setup();
    renderComposer({
      api: fakeApi({ sendFileContext, sendMessage }),
      fileApi: fakeFileApi({ upload }),
      projectId: "energy",
    });
    const file = new File(["brief"], "C:\\private\\brief.txt", { type: "text/plain" });

    await user.upload(screen.getByLabelText("选择本地文件"), file);
    await screen.findByRole("list", { name: "待发送附件" });
    await user.type(screen.getByRole("textbox", { name: "消息" }), "review");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(sendMessage).toHaveBeenCalledOnce());
    expect(upload).toHaveBeenCalledWith(
      "energy", "Inputs/brief.txt", file, "reject", undefined, expect.any(AbortSignal),
    );
    expect(sendFileContext).toHaveBeenCalledWith(
      "energy",
      "main",
      { file: "Inputs/brief.txt", type: "file" },
      expect.any(String),
    );
    expect(sendMessage).toHaveBeenCalledWith("energy", "main", "review", expect.any(String), expect.any(String));
    expect(JSON.stringify([upload.mock.calls, sendFileContext.mock.calls, sendMessage.mock.calls])).not.toContain("C:\\private");
  });

  it("aborts old uploads and clears attachments when project or session changes", async () => {
    let oldSignal: AbortSignal | undefined;
    const upload = vi.fn((_projectId, _path, _file, _conflict, _revision, signal?: AbortSignal) => {
      oldSignal = signal;
      return new Promise<FileEntry>((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")));
      });
    });
    const user = userEvent.setup();
    const { props, view } = renderComposer({ fileApi: fakeFileApi({ upload }) });

    await user.upload(screen.getByLabelText("选择本地文件"), new File(["old"], "old.txt"));
    await waitFor(() => expect(upload).toHaveBeenCalledOnce());
    view.rerender(<MessageComposer {...props} projectId="project-2" sessionId="s2" />);

    expect(oldSignal?.aborted).toBe(true);
    expect(screen.queryByRole("list", { name: "待发送附件" })).not.toBeInTheDocument();
  });

  it("does not render server, editor, or selection reference controls", () => {
    renderComposer();

    expect(screen.queryByText("引用服务器文件")).not.toBeInTheDocument();
    expect(screen.queryByText("引用当前编辑文件")).not.toBeInTheDocument();
    expect(screen.queryByText("引用当前选区")).not.toBeInTheDocument();
  });

  it("reuses the same idempotency key when retrying one failed send", async () => {
    const sendMessage = vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ commandId: "cmd", status: "accepted" });
    const user = userEvent.setup();
    renderComposer({ api: fakeApi({ sendMessage }) });

    await user.type(screen.getByRole("textbox", { name: "消息" }), "hello");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("发送失败");
    await user.click(screen.getByRole("button", { name: "重试发送" }));

    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(2));
    expect(sendMessage.mock.calls[0]?.[3]).toBe(sendMessage.mock.calls[1]?.[3]);
    expect(sendMessage.mock.calls[0]?.[4]).toBe(sendMessage.mock.calls[1]?.[4]);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("");
  });

  it("invalidates a failed attachment attempt when the user removes its chip", async () => {
    let failSend!: () => void;
    const sendMessage = vi.fn(() => new Promise<never>((_resolve, reject) => {
      failSend = () => reject(new Error("offline"));
    }));
    const user = userEvent.setup();
    renderComposer({ api: fakeApi({ sendMessage }) });

    await user.upload(screen.getByLabelText("选择本地文件"), new File(["brief"], "brief.txt"));
    await screen.findByRole("list", { name: "待发送附件" });
    await user.type(screen.getByRole("textbox", { name: "消息" }), "review");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(screen.getByRole("button", { name: "移除 brief.txt" })).toBeDisabled();
    failSend();
    expect(await screen.findByRole("button", { name: "重试发送" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "移除 brief.txt" }));

    expect(screen.queryByRole("button", { name: "重试发送" })).not.toBeInTheDocument();
  });

  it("executes slash history and stop without sending them as messages", async () => {
    const interrupt = vi.fn().mockResolvedValue({ commandId: "stop", status: "accepted" });
    const sendMessage = vi.fn();
    const onHistoryRequested = vi.fn();
    const user = userEvent.setup();
    renderComposer({ api: fakeApi({ interrupt, sendMessage }), onHistoryRequested });
    const view = screen.getByRole("textbox", { name: "消息" });

    await user.type(view, "/history");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(onHistoryRequested).toHaveBeenCalledOnce();
    fireEvent.change(view, { target: { value: "/stop" } });
    fireEvent.keyDown(view, { key: "Enter" });
    await waitFor(() => expect(interrupt).toHaveBeenCalledOnce());
    expect(interrupt).toHaveBeenCalledWith("main", expect.any(String));
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("blocks duplicate asynchronous slash create commands and carries project identity", async () => {
    let finishCreate!: () => void;
    const create = vi.fn(() => new Promise<Awaited<ReturnType<ConversationApi["create"]>>>((resolve) => {
      finishCreate = () => resolve({
        active: true, name: "新会话", preview: "", projectId: "energy", sessionId: "s2", timestamp: "now",
      });
    }));
    const user = userEvent.setup();
    renderComposer({ api: fakeApi({ create }), projectId: "energy" });

    await user.type(screen.getByRole("textbox", { name: "消息" }), "/new");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "消息" }), { key: "Enter" });
    expect(create).toHaveBeenCalledOnce();
    expect(create).toHaveBeenCalledWith("energy", "新会话", "main");
    finishCreate();
  });
});
