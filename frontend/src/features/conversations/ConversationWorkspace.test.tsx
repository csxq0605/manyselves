import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { ConversationWorkspace } from "./ConversationWorkspace";

describe("ConversationWorkspace", () => {
  it("loads the authoritative active conversation and history", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return { messages: [{ content: "server history", message_id: "m1", role: "agent" }], sessionId: "s1" };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "s1",
          conversations: [{ active: true, name: "需求梳理", preview: "server", sessionId: "s1", timestamp: "now" }],
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
      <ConversationWorkspace agentId="main" currentEditorPath="src/main.py"
        gateway={gateway} projectId="project-1" />
    </QueryClientProvider>);

    expect(await screen.findByText("server history")).toBeVisible();
    expect(screen.getByRole("button", { name: /需求梳理/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("option", { name: "Inputs/brief.md" })).toBeVisible();
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
    expect(await screen.findByRole("heading", { name: "项目一会话" })).toBeVisible();

    view.rerender(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={gateway} projectId="project-2" />
    </QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "项目二会话" })).toBeVisible();
    expect(screen.queryByText("项目一会话")).not.toBeInTheDocument();
  });
});
