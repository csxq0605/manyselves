import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConversationActions } from "./ConversationActions";
import type { ConversationApi, ConversationListSnapshot, ConversationSummary } from "./conversation-api";
import { ConversationList } from "./ConversationList";

const sessions: ConversationSummary[] = [
  { active: true, name: "需求梳理", preview: "first", projectId: "project-1", sessionId: "s1", timestamp: "2026-08-03T00:00:00Z" },
  { active: false, name: "方案评审", preview: "second", projectId: "project-1", sessionId: "s2", timestamp: "2026-08-03T00:01:00Z" },
];

function snapshot(activeSessionId = "s1"): ConversationListSnapshot {
  return { activeSessionId, conversations: sessions.map((item) => ({
    ...item, active: item.sessionId === activeSessionId,
  })), projectId: "project-1" };
}

function renderWithQuery(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function fakeApi(overrides: Partial<ConversationApi> = {}): ConversationApi {
  return {
    activate: async (_projectId, sessionId) => sessions.find((item) => item.sessionId === sessionId)!,
    clear: async (projectId) => ({ activeSessionId: "s3", projectId }),
    create: async (projectId, name) => ({ ...sessions[0]!, active: true, name, projectId, sessionId: "s3" }),
    delete: async (projectId) => ({ activeSessionId: "s1", projectId }),
    editResend: async () => ({ commandId: crypto.randomUUID(), status: "accepted" }),
    interrupt: async () => ({ commandId: crypto.randomUUID(), status: "accepted" }),
    list: async () => snapshot(),
    messages: async (projectId) => ({ messages: [], projectId, sessionId: "s1" }),
    rename: async (projectId, sessionId, name) => ({ ...sessions[0]!, name, projectId, sessionId }),
    rollback: async () => ({ commandId: crypto.randomUUID(), conversationHistory: [], restoredFiles: 0, status: "accepted" }),
    sendFileContext: async () => ({ commandId: crypto.randomUUID(), status: "accepted" }),
    sendMessage: async () => ({ commandId: crypto.randomUUID(), status: "accepted" }),
    ...overrides,
  };
}

describe("ConversationList", () => {
  it("switches only after server activation succeeds", async () => {
    let resolveActivation!: (value: ConversationSummary) => void;
    const activate = vi.fn(() => new Promise<ConversationSummary>((resolve) => {
      resolveActivation = resolve;
    }));
    const onActivated = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<ConversationList agentId="main" api={fakeApi({ activate })} onActivated={onActivated} projectId="project-1" />);

    await user.click(await screen.findByRole("button", { name: /方案评审/ }));
    expect(screen.getByRole("button", { name: /需求梳理/ })).toHaveAttribute("aria-current", "true");
    expect(onActivated).not.toHaveBeenCalled();

    resolveActivation({ ...sessions[1]!, active: true });
    expect(await screen.findByText("方案评审 已激活")).toBeVisible();
    expect(onActivated).toHaveBeenCalledWith("s2");
  });

  it("keeps the active conversation when activation fails", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ConversationList agentId="main" api={fakeApi({
      activate: async () => { throw new Error("busy"); },
    })} projectId="project-1" />);

    await user.click(await screen.findByRole("button", { name: /方案评审/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("会话切换失败");
    expect(screen.getByRole("button", { name: /需求梳理/ })).toHaveAttribute("aria-current", "true");
  });
});

describe("ConversationActions", () => {
  it("creates and renames conversations through in-page forms", async () => {
    const effects: string[] = [];
    const onChanged = vi.fn();
    const api = fakeApi({
      create: async (projectId, name) => { effects.push(`create:${name}`); return { ...sessions[0]!, name, projectId, sessionId: "s3" }; },
      rename: async (projectId, id, name) => { effects.push(`rename:${id}:${name}`); return { ...sessions[0]!, name, projectId, sessionId: id }; },
    });
    const user = userEvent.setup();

    render(<ConversationActions activeConversation={sessions[1]!} agentId="main" api={api} onChanged={onChanged} projectId="project-1" />);
    await user.click(screen.getByRole("button", { name: "新建会话" }));
    expect(screen.getByRole("form", { name: "新建会话" })).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "会话名称" }), "  新会话  ");
    await user.click(screen.getByRole("button", { name: "确认新建" }));
    await waitFor(() => expect(effects).toContain("create:新会话"));
    expect(onChanged).toHaveBeenLastCalledWith("s3", expect.objectContaining({ name: "新会话", sessionId: "s3" }));

    await user.click(screen.getByRole("button", { name: "重命名会话" }));
    expect(screen.getByRole("form", { name: "重命名会话" })).toBeVisible();
    const rename = screen.getByRole("textbox", { name: "会话名称" });
    await user.clear(rename);
    await user.type(rename, "  新名称  ");
    await user.click(screen.getByRole("button", { name: "确认重命名" }));
    await waitFor(() => expect(effects).toContain("rename:s2:新名称"));
    expect(onChanged).toHaveBeenLastCalledWith("s2", expect.objectContaining({ name: "新名称", sessionId: "s2" }));

    expect(effects).toEqual(["create:新会话", "rename:s2:新名称"]);
  });

  it("confirms destructive actions inside the page without browser dialogs", async () => {
    const effects: string[] = [];
    const api = fakeApi({
      clear: async (projectId) => { effects.push("clear"); return { activeSessionId: "s3", projectId }; },
      delete: async (projectId, id) => { effects.push(`delete:${id}`); return { activeSessionId: "s1", projectId }; },
    });
    const prompt = vi.spyOn(window, "prompt");
    const confirm = vi.spyOn(window, "confirm");
    const user = userEvent.setup();

    render(<ConversationActions activeConversation={sessions[1]!} agentId="main" api={api} projectId="project-1" />);
    await user.click(screen.getByRole("button", { name: "删除会话" }));
    expect(screen.getByRole("group", { name: "删除会话确认" })).toHaveTextContent("方案评审");
    await user.click(screen.getByRole("button", { name: "取消删除" }));
    expect(effects).toEqual([]);
    await user.click(screen.getByRole("button", { name: "删除会话" }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(effects).toContain("delete:s2"));

    await user.click(screen.getByRole("button", { name: "清空当前会话" }));
    expect(screen.getByRole("group", { name: "清空会话确认" })).toHaveTextContent("方案评审");
    await user.click(screen.getByRole("button", { name: "确认清空" }));
    await waitFor(() => expect(effects).toContain("clear"));

    expect(effects).toEqual(["delete:s2", "clear"]);
    expect(prompt).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
  });
});
