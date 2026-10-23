import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { AppProviders } from "../../app/providers";
import { HistoryPage, HistorySessionRoutePage } from "./HistoryPage";

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function gatewayWithConversations(count: number): ApiGateway {
  return {
    baseUrl: "",
    clientId: "test-client",
    bootstrap: async () => { throw new Error("unused"); },
    controlLease: {
      release: async () => undefined,
      start: () => undefined,
      stop: () => undefined,
    },
    requestBlob: async () => { throw new Error("unused"); },
    requestJson: async <T,>(path: string): Promise<T> => {
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "session-1",
          conversations: Array.from({ length: count }, (_, index) => ({
            active: index === 0,
            messageCount: 2,
            name: `Session ${index + 1}`,
            preview: `Preview ${index + 1}`,
            projectId: "project-1",
            sessionId: `session-${index + 1}`,
            timestamp: `2026-08-05T08:${String(index).padStart(2, "0")}:00Z`,
          })),
          projectId: "project-1",
        } as T;
      }
      throw new Error(`unexpected request: ${path}`);
    },
    requestVoid: async () => undefined,
    sendMessage: async () => { throw new Error("unused"); },
  };
}

function renderHistory(count: number) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <AppProviders queryClient={queryClient}>
      <MemoryRouter>
        <HistoryPage gateway={gatewayWithConversations(count)} projectId="project-1" />
      </MemoryRouter>
    </AppProviders>,
  );
}

function readOnlyGateway(requests: string[]): ApiGateway {
  return {
    ...gatewayWithConversations(0),
    requestJson: async <T,>(path: string): Promise<T> => {
      requests.push(path);
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "",
          conversations: [{
            active: false, name: "上次运行", preview: "开始报告", projectId: "run-f",
            sessionId: "old-run", timestamp: "2026-10-23T10:00:00Z",
          }],
          projectId: "run-f",
          readOnly: true,
        } as T;
      }
      if (path.startsWith("/api/v1/conversations/old-run/messages?")) {
        return {
          messages: [
            { message_id: "m1", role: "user", content: "开始报告", ts: "2026-10-23T10:00:00Z" },
            { role: "agent", content: "已开始", ts: "2026-10-23T10:00:05Z" },
          ],
          projectId: "run-f",
          sessionId: "old-run",
        } as T;
      }
      throw new Error(`unexpected request: ${path}`);
    },
  };
}

describe("HistoryPage", () => {
  it("opens another project's conversation to read, without switching anything", async () => {
    const user = userEvent.setup();
    const requests: string[] = [];
    const gateway = readOnlyGateway(requests);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AppProviders queryClient={queryClient}>
        <MemoryRouter initialEntries={["/projects/run-f/history"]}>
          <Routes>
            <Route path="/projects/:projectId/history" element={<HistoryPage gateway={gateway} projectId="run-f" />} />
            <Route path="/projects/:projectId/history/:sessionId" element={<HistorySessionRoutePage gateway={gateway} />} />
          </Routes>
          <LocationProbe />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByRole("note")).toHaveTextContent("只读");
    expect(screen.queryByRole("button", { name: "新对话" })).not.toBeInTheDocument();
    await user.click(screen.getByText("上次运行"));

    expect(await screen.findByText("已开始")).toBeVisible();
    expect(screen.getByTestId("location")).toHaveTextContent("/projects/run-f/history/old-run");
    expect(screen.queryByRole("button", { name: /重编辑/ })).not.toBeInTheDocument();
    expect(requests.some((path) => path.includes("/activate"))).toBe(false);
  });

  it("paginates long conversation history instead of rendering every session at once", async () => {
    const user = userEvent.setup();
    renderHistory(25);

    const list = await screen.findByRole("list", { name: "history-conversation-list" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(20);
    expect(screen.getByText("Session 1")).toBeVisible();
    expect(screen.queryByText("Session 21")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "\u4e0b\u4e00\u9875" }));

    expect(within(list).getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByText("Session 21")).toBeVisible();
    expect(screen.getByText("Page 2 / 2")).toBeVisible();
  });

  it("shows a disabled pagination bar even when history fits on one page", async () => {
    renderHistory(5);

    await screen.findByRole("list", { name: "history-conversation-list" });

    expect(screen.getByText("Page 1 / 1")).toBeVisible();
    expect(screen.getByRole("button", { name: "\u4e0a\u4e00\u9875" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "\u4e0b\u4e00\u9875" })).toBeDisabled();
  });

  it("formats conversation timestamps in Asia/Shanghai", async () => {
    renderHistory(1);

    expect(await screen.findByText("2026-08-05 16:00:00")).toBeVisible();
  });

  it("puts a newly created history conversation into the shared cache before navigating", async () => {
    const requestJson = async <T,>(path: string, init?: { readonly method?: string }): Promise<T> => {
      if (path.startsWith("/api/v1/conversations?") && !init?.method) return {
        activeSessionId: "session-1",
        conversations: [{ active: true, name: "Session 1", preview: "", projectId: "project-1", sessionId: "session-1", timestamp: "before" }],
        projectId: "project-1",
      } as T;
      if (path === "/api/v1/conversations" && init?.method === "POST") return {
        active: true,
        name: "新对话",
        preview: "",
        projectId: "project-1",
        sessionId: "session-new",
        timestamp: "now",
      } as T;
      throw new Error(`unexpected request: ${path}`);
    };
    const gateway = { ...gatewayWithConversations(1), requestJson } as ApiGateway;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 300_000 } } });
    const user = userEvent.setup();
    render(<AppProviders queryClient={queryClient}>
      <MemoryRouter initialEntries={["/projects/project-1/history"]}>
        <HistoryPage gateway={gateway} projectId="project-1" />
        <LocationProbe />
      </MemoryRouter>
    </AppProviders>);
    await screen.findByText("Session 1");

    await user.click(screen.getByRole("button", { name: "新对话" }));

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/project-1/conversations/session-new"));
    expect(queryClient.getQueryData<{ readonly activeSessionId: string }>(["conversations", "project-1", "main"])?.activeSessionId).toBe("session-new");
  });
});
