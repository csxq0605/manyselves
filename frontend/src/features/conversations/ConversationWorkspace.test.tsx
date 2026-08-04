import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { ConversationWorkspace } from "./ConversationWorkspace";

describe("ConversationWorkspace", () => {
  it("loads the authoritative active conversation and history", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return { messages: [{ content: "server history", message_id: "m1", role: "agent" }], projectId: "project-1", sessionId: "s1" };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "s1",
          conversations: [{ active: true, name: "需求梳理", preview: "server", projectId: "project-1", sessionId: "s1", timestamp: "now" }],
          projectId: "project-1",
        };
      }
      if (path.includes("/files/tree")) {
        return { entries: [{ kind: "file", modifiedAt: "now", name: "brief.md", path: "Inputs/brief.md", revision: "r", size: 1 }] };
      }
      throw new Error(`unexpected path: ${path}`);
    });
    const gateway = { requestJson } as unknown as ApiGateway;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={gateway} projectId="project-1" />
    </QueryClientProvider>);

    expect(await screen.findByText("server history")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "需求梳理" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /需求梳理/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传本地文件" })).toBeVisible();
    expect(screen.queryByRole("option", { name: "Inputs/brief.md" })).not.toBeInTheDocument();
  });

  it("does not reuse a warm conversation cache after the project changes", async () => {
    let conversationRead = 0;
    const requestJson = vi.fn(async (path: string) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return { messages: [], sessionId: `s${conversationRead}` };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        conversationRead += 1;
        return {
          activeSessionId: `s${conversationRead}`,
          conversations: [{
            active: true,
            name: conversationRead === 1 ? "项目一会话" : "项目二会话",
            preview: "",
            sessionId: `s${conversationRead}`,
            timestamp: "now",
          }],
        };
      }
      if (path.includes("/files/tree")) return { entries: [] };
      throw new Error(`unexpected path: ${path}`);
    });
    const gateway = { requestJson } as unknown as ApiGateway;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
    const view = render(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={gateway} projectId="project-1" />
    </QueryClientProvider>);
    expect(await screen.findByText("今天要处理什么？")).toBeVisible();

    view.rerender(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={gateway} projectId="project-2" />
    </QueryClientProvider>);

    expect(await screen.findByText("当前对话属于“project-2”项目。Agent 会自动使用项目输入、知识库与输出模板。")).toBeVisible();
    expect(screen.queryByText("项目一会话")).not.toBeInTheDocument();
  });

  it("uses the confirmed Codex-like empty conversation composition", async () => {
    const user = userEvent.setup();
    const requestJson = vi.fn(async (path: string) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return { messages: [], projectId: "project-1", sessionId: "s1" };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "s1",
          conversations: [{ active: true, name: "新会话", preview: "", projectId: "project-1", sessionId: "s1", timestamp: "now" }],
          projectId: "project-1",
        };
      }
      throw new Error(`unexpected path: ${path}`);
    });
    const gateway = { requestJson } as unknown as ApiGateway;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={gateway} projectId="project-1" projectName="能源管理" />
    </QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "今天要处理什么？" })).toBeVisible();
    expect(screen.getByText("当前对话属于“能源管理”项目。Agent 会自动使用项目输入、知识库与输出模板。")).toBeVisible();
    expect(screen.queryByRole("region", { name: "会话列表" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "会话操作" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "更多会话操作" }));
    expect(screen.getByRole("region", { name: "会话列表" })).toBeVisible();
    expect(screen.getByRole("group", { name: "会话操作" })).toBeVisible();
  });

  it("withholds the old composer while a requested session is activating", async () => {
    let finishActivation!: () => void;
    const activation = new Promise<Record<string, unknown>>((resolve) => {
      finishActivation = () => resolve({ active: true, name: "目标会话", preview: "", projectId: "project-1", sessionId: "s2", timestamp: "now" });
    });
    const requestJson = vi.fn(async (path: string, init?: { readonly method?: string }) => {
      if (path.startsWith("/api/v1/conversations?")) return {
        activeSessionId: "s1",
        conversations: [
          { active: true, name: "旧会话", preview: "", projectId: "project-1", sessionId: "s1", timestamp: "now" },
          { active: false, name: "目标会话", preview: "", projectId: "project-1", sessionId: "s2", timestamp: "now" },
        ],
        projectId: "project-1",
      };
      if (path.includes("/s2/activate") && init?.method === "POST") return activation;
      if (path.startsWith("/api/v1/conversations/messages")) return { messages: [], projectId: "project-1", sessionId: "s2" };
      throw new Error(`unexpected path: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<StrictMode><QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={{ requestJson } as unknown as ApiGateway} projectId="project-1" requestedSessionId="s2" />
    </QueryClientProvider></StrictMode>);

    expect(await screen.findByRole("status")).toHaveTextContent("正在切换会话");
    expect(screen.queryByRole("button", { name: "上传本地文件" })).not.toBeInTheDocument();
    expect(requestJson.mock.calls.some(([path]) => String(path).includes("/messages"))).toBe(false);
    finishActivation();
    expect(await screen.findByRole("button", { name: "上传本地文件" })).toBeVisible();
  });

  it("reactivates a session after an external switch instead of reusing a settled promise", async () => {
    const requestJson = vi.fn(async (path: string, init?: { readonly method?: string }) => {
      if (path.startsWith("/api/v1/conversations?")) return {
        activeSessionId: "s1",
        conversations: [
          { active: true, name: "会话一", preview: "", projectId: "project-1", sessionId: "s1", timestamp: "now" },
          { active: false, name: "会话二", preview: "", projectId: "project-1", sessionId: "s2", timestamp: "now" },
        ],
        projectId: "project-1",
      };
      if (path.includes("/s2/activate") && init?.method === "POST") return {
        active: true, name: "会话二", preview: "", projectId: "project-1", sessionId: "s2", timestamp: "now",
      };
      if (path.startsWith("/api/v1/conversations/messages")) return { messages: [], projectId: "project-1", sessionId: "s2" };
      throw new Error(`unexpected path: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
    const renderWorkspace = (requestedSessionId: string) => <QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={{ requestJson } as unknown as ApiGateway} projectId="project-1" requestedSessionId={requestedSessionId} />
    </QueryClientProvider>;
    const view = render(renderWorkspace("s2"));

    await screen.findByRole("button", { name: "上传本地文件" });
    expect(requestJson.mock.calls.filter(([path]) => String(path).includes("/s2/activate"))).toHaveLength(1);
    act(() => client.setQueryData(["conversations", "project-1", "main"], {
      activeSessionId: "s1",
      conversations: [
        { active: true, name: "会话一", preview: "", projectId: "project-1", sessionId: "s1", timestamp: "now" },
        { active: false, name: "会话二", preview: "", projectId: "project-1", sessionId: "s2", timestamp: "now" },
      ],
      projectId: "project-1",
    }));
    view.rerender(renderWorkspace("s1"));
    view.rerender(renderWorkspace("s2"));

    await waitFor(() => expect(
      requestJson.mock.calls.filter(([path]) => String(path).includes("/s2/activate")),
    ).toHaveLength(2));
  });
});
