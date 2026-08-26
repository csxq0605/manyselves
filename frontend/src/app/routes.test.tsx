import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../api/gateway";
import { useConversationStore } from "../store/conversation-store";
import { AppRoutes } from "./routes";
import { AppProviders } from "./providers";

afterEach(() => {
  useConversationStore.setState({ activeSessionIds: {} });
});

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function renderRoutes(
  savedRoute: string | null,
  options: { readonly conversations?: readonly { readonly sessionId: string }[]; readonly projectId?: string; readonly projects?: readonly { readonly active: boolean; readonly id: string }[] } = {},
) {
  window.localStorage.clear();
  if (savedRoute) window.localStorage.setItem("manyselves.lastProjectRoute.v1", savedRoute);
  const projects = options.projects ?? [{ active: true, id: "project-1" }];
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["bootstrap"], { conversations: options.conversations ?? [], project: { id: options.projectId ?? "project-1" } });
  const gateway = { requestJson: vi.fn().mockResolvedValue({ projects: projects.map((project) => ({ ...project, description: "", displayName: project.id, revision: "r1" })) }) } as unknown as ApiGateway;
  return render(<AppProviders queryClient={queryClient}><MemoryRouter initialEntries={["/"]}><AppRoutes gateway={gateway} /><LocationProbe /></MemoryRouter></AppProviders>);
}

describe("authenticated root route restore", () => {
  it("restores a saved project conversation when its project still exists", async () => {
    const view = renderRoutes("/projects/project-1/conversations/conversation-1", { conversations: [{ sessionId: "conversation-1" }] });

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/projects/project-1/conversations/conversation-1"));
  });

  it("falls back to the active project for malformed or stale saved routes", async () => {
    const view = renderRoutes("/settings/models");

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/projects/project-1"));
  });

  it("falls back to the active project for a saved project that no longer exists", async () => {
    const view = renderRoutes("/projects/deleted-project/conversations/conversation-1");

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/projects/project-1"));
  });

  it("restores the project home when the saved conversation was deleted or is the new placeholder", async () => {
    const deleted = renderRoutes("/projects/project-1/conversations/deleted", { conversations: [{ sessionId: "conversation-1" }] });
    await waitFor(() => expect(deleted.getByTestId("location")).toHaveTextContent("/projects/project-1"));
    deleted.unmount();
    const placeholder = renderRoutes("/projects/project-1/conversations/new", { conversations: [{ sessionId: "new" }] });
    await waitFor(() => expect(placeholder.getByTestId("location")).toHaveTextContent("/projects/project-1"));
  });

  it("restores the non-current project's home when its saved conversation cannot be validated", async () => {
    const view = renderRoutes("/projects/project-2/conversations/conversation-2", {
      conversations: [{ sessionId: "conversation-2" }],
      projectId: "project-1",
      projects: [{ active: true, id: "project-1" }, { active: false, id: "project-2" }],
    });

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/projects/project-2"));
  });

  it("waits for bootstrap conversation identity before replacing a saved conversation", async () => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "manyselves.lastProjectRoute.v1",
      "/projects/project-1/conversations/conversation-1",
    );
    let resolveBootstrap!: (value: unknown) => void;
    const bootstrap = new Promise((resolve) => { resolveBootstrap = resolve; });
    const gateway = {
      bootstrap: vi.fn(() => bootstrap),
      requestJson: vi.fn().mockResolvedValue({
        projects: [{
          active: true,
          description: "",
          displayName: "project-1",
          id: "project-1",
          revision: "r1",
        }],
      }),
    } as unknown as ApiGateway;
    const view = render(
      <AppProviders>
        <MemoryRouter initialEntries={["/"]}>
          <AppRoutes gateway={gateway} />
          <LocationProbe />
        </MemoryRouter>
      </AppProviders>,
    );

    await waitFor(() => expect(gateway.requestJson).toHaveBeenCalled());
    expect(view.getByTestId("location")).toHaveTextContent("/");
    resolveBootstrap({
      conversations: [{ sessionId: "conversation-1" }],
      project: { id: "project-1" },
    });

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/conversations/conversation-1",
    ));
  });
});

describe("project directory routes", () => {
  it.each([
    ["inputs", "输入", "Inputs"],
    ["knowledge", "知识库", "Knowledge"],
    ["templates", "输出模板", "Templates"],
    ["outputs", "输出", "Outputs"],
  ] as const)("mounts the shared %s directory page", async (section, title, root) => {
    const requestJson = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") {
        return { projects: [{ active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" }] };
      }
      if (path.includes("/files/tree")) return { entries: [] };
      throw new Error(`unexpected request: ${path}`);
    });
    const gateway = { requestJson } as unknown as ApiGateway;

    render(
      <AppProviders>
        <MemoryRouter initialEntries={[`/projects/project-1/${section}`]}>
          <AppRoutes gateway={gateway} />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByRole("heading", { name: title }, { timeout: 15_000 })).toBeVisible();
    await waitFor(() => {
      expect(requestJson).toHaveBeenCalledWith(
        `/api/v1/projects/project-1/files/tree?path=${root}`,
      );
    });
  }, 20_000);
});

describe("global knowledge route", () => {
  it("mounts the real global knowledge workspace", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" }],
      };
      if (path === "/api/v1/global-knowledge/files/tree?path=") return { entries: [] };
      throw new Error(`unexpected request: ${path}`);
    });

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/knowledge"]}>
          <AppRoutes gateway={{ requestJson } as unknown as ApiGateway} />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByRole("heading", { name: "全局知识库" }, { timeout: 15_000 })).toBeVisible();
    await waitFor(() => expect(requestJson).toHaveBeenCalledWith(
      "/api/v1/global-knowledge/files/tree?path=",
    ));
  }, 20_000);
});

describe("project operations routes", () => {
  const runtime = {
    active_session_id: "session-1",
    agent_statuses: { main: "running" },
    checkpoints: [],
    controller_client_id: null,
    debug: [],
    queues: [],
    ready: true,
    tasks: [],
    tools: [],
    workspace: null,
  };

  function gateway(bootstrapProjectId = "project-1") {
    const requestJson = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" }],
      };
      if (path === "/api/v1/events/logs?projectId=project-1&limit=200") return {
        entries: [{ agentId: "main", eventId: "stream-1:evt-1", level: "info", message: "任务开始", sessionId: "session-1", timestamp: "2026-08-04T08:00:00Z", type: "task.status.changed" }],
        projectId: "project-1",
      };
      throw new Error(`unexpected request: ${path}`);
    });
    return {
      bootstrap: vi.fn().mockResolvedValue({ project: { id: bootstrapProjectId }, runtime }),
      requestJson,
    } as unknown as ApiGateway;
  }

  it("mounts the real runtime page from the sanitized bootstrap snapshot", async () => {
    render(<AppProviders><MemoryRouter initialEntries={["/projects/project-1/runtime"]}><AppRoutes gateway={gateway()} /></MemoryRouter></AppProviders>);

    expect(await screen.findByRole("heading", { name: "运行态" }, { timeout: 10_000 })).toBeVisible();
    expect(screen.queryByText("此项目区域将在后续工作中提供受控内容。")).not.toBeInTheDocument();
  }, 15_000);

  it("refuses to render runtime data from a different active project", async () => {
    render(<AppProviders><MemoryRouter initialEntries={["/projects/project-1/runtime"]}><AppRoutes gateway={gateway("project-2")} /></MemoryRouter></AppProviders>);

    expect(await screen.findByRole("alert", {}, { timeout: 10_000 })).toHaveTextContent("运行态项目校验失败");
    expect(screen.queryByRole("heading", { name: "运行态" })).not.toBeInTheDocument();
  }, 15_000);

  it("mounts the project-scoped log projection", async () => {
    const api = gateway();
    render(<AppProviders><MemoryRouter initialEntries={["/projects/project-1/logs"]}><AppRoutes gateway={api} /></MemoryRouter></AppProviders>);

    expect(await screen.findByText("任务开始", {}, { timeout: 10_000 })).toBeVisible();
    expect(api.requestJson).toHaveBeenCalledWith("/api/v1/events/logs?projectId=project-1&limit=200");
  }, 15_000);
});

describe("project conversation routes", () => {
  function conversationGateway(initiallyActive = true) {
    let projectActive = initiallyActive;
    const requestJson = vi.fn(async (path: string, init?: { readonly method?: string }) => {
      if (path === "/api/v1/projects") {
        return { projects: [{ active: projectActive, description: "", displayName: "Project 1", id: "project-1", revision: "r1" }] };
      }
      if (path === "/api/v1/projects/project-1/activate" && init?.method === "POST") {
        projectActive = true;
        return { active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r2" };
      }
      if (path === "/api/v1/conversations" && init?.method === "POST") {
        if (!projectActive) throw new Error("project not active");
        return { active: true, name: "新会话", preview: "", projectId: "project-1", sessionId: "s-new", timestamp: "now" };
      }
      if (path === "/api/v1/conversations?projectId=project-1&agentId=main") {
        if (!projectActive) throw new Error("project not active");
        return {
          activeSessionId: "s-new",
          conversations: [{ active: true, name: "需求梳理", preview: "", projectId: "project-1", sessionId: "s-new", timestamp: "now" }],
          projectId: "project-1",
        };
      }
      if (path === "/api/v1/conversations/messages?projectId=project-1&agentId=main") {
        return { messages: [], projectId: "project-1", sessionId: "s-new" };
      }
      throw new Error(`unexpected request: ${path}`);
    });
    return { gateway: { baseUrl: "https://api.example", requestJson } as unknown as ApiGateway, requestJson };
  }

  it("mounts the real project-bound conversation workspace", async () => {
    const { gateway } = conversationGateway();
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/projects/project-1/conversations/s-new"]}>
          <AppRoutes gateway={gateway} />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByRole("heading", { name: "今天要处理什么？" })).toBeVisible();
    expect(screen.getByText("当前对话属于“Project 1”项目。Agent 会自动使用项目输入、知识库与输出模板。")).toBeVisible();
    expect(screen.getByRole("button", { name: "上传本地文件" })).toBeVisible();
    expect(screen.queryByText("此项目区域将在后续工作中提供受控内容。")).not.toBeInTheDocument();
  });

  it("creates a conversation for the current project from project home and replaces the new route", async () => {
    const { gateway, requestJson } = conversationGateway();
    const user = userEvent.setup();
    render(
      <StrictMode><AppProviders>
        <MemoryRouter initialEntries={["/projects/project-1"]}>
          <AppRoutes gateway={gateway} />
          <LocationProbe />
        </MemoryRouter>
      </AppProviders></StrictMode>,
    );

    await user.click(screen.getByRole("button", { name: "新对话" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/conversations/s-new",
    ));
    expect(requestJson).toHaveBeenCalledWith("/api/v1/conversations", {
      json: { agentId: "main", name: "新会话", projectId: "project-1" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson.mock.calls.filter(([path]) => path === "/api/v1/conversations")).toHaveLength(1);
  });

  it("keeps a newly created session valid while an older conversation list is being refreshed", async () => {
    let finishConversationRefresh!: () => void;
    const conversationRefresh = new Promise<Record<string, unknown>>((resolve) => {
      finishConversationRefresh = () => resolve({
        activeSessionId: "s-old",
        conversations: [{ active: true, name: "旧会话", preview: "", projectId: "project-1", sessionId: "s-old", timestamp: "before" }],
        projectId: "project-1",
      });
    });
    const requestJson = vi.fn(async (path: string, init?: { readonly method?: string }): Promise<unknown> => {
      if (path === "/api/v1/projects") {
        return { projects: [{ active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" }] };
      }
      if (path === "/api/v1/conversations" && init?.method === "POST") {
        return { active: true, name: "新会话", preview: "", projectId: "project-1", sessionId: "s-new", timestamp: "now" };
      }
      if (path === "/api/v1/conversations?projectId=project-1&agentId=main") {
        return {
          activeSessionId: "s-new",
          conversations: [
            { active: true, name: "新会话", preview: "", projectId: "project-1", sessionId: "s-new", timestamp: "now" },
            { active: false, name: "旧会话", preview: "", projectId: "project-1", sessionId: "s-old", timestamp: "before" },
          ],
          projectId: "project-1",
        };
      }
      if (path === "/api/v1/conversations/messages?projectId=project-1&agentId=main") {
        return { messages: [], projectId: "project-1", sessionId: "s-new" };
      }
      throw new Error(`unexpected request: ${path}`);
    });
    const gateway = { baseUrl: "https://api.example", requestJson } as unknown as ApiGateway;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const refreshSettled = queryClient.fetchQuery({
      queryFn: () => conversationRefresh,
      queryKey: ["conversations", "project-1", "main"],
      staleTime: 0,
    }).catch(() => undefined);
    const user = userEvent.setup();
    render(<AppProviders queryClient={queryClient}>
      <MemoryRouter initialEntries={["/projects/project-1"]}>
        <AppRoutes gateway={gateway} />
        <LocationProbe />
      </MemoryRouter>
    </AppProviders>);

    await user.click(screen.getByRole("button", { name: "新对话" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/project-1/conversations/s-new"));
    expect(screen.queryByText("该会话不属于当前项目")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传本地文件" })).toBeVisible();
    finishConversationRefresh();
    await refreshSettled;
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/project-1/conversations/s-new"));
    expect(queryClient.getQueryData<{ readonly activeSessionId: string }>(["conversations", "project-1", "main"])?.activeSessionId).toBe("s-new");
    await waitFor(() => expect(queryClient.getQueryData<{
      readonly conversations: readonly { readonly sessionId: string }[];
    }>(["conversations", "project-1", "main"])?.conversations.map(
      (conversation) => conversation.sessionId,
    )).toEqual(["s-new", "s-old"]));
  });

  it("clears an invalid persisted session and returns to the project home", async () => {
    useConversationStore.getState().setActiveSession("project-1", "stale-session");
    const { gateway } = conversationGateway();
    render(<AppProviders>
      <MemoryRouter initialEntries={["/projects/project-1/conversations/stale-session"]}>
        <AppRoutes gateway={gateway} />
        <LocationProbe />
      </MemoryRouter>
    </AppProviders>);

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/projects\/project-1$/));
    expect(useConversationStore.getState().getActiveSession("project-1")).toBeNull();
    expect(screen.queryByText("该会话不属于当前项目")).not.toBeInTheDocument();
  });

  it("activates an inactive route project before loading its conversation", async () => {
    const { gateway, requestJson } = conversationGateway(false);
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/projects/project-1/conversations/s-new"]}>
          <AppRoutes gateway={gateway} />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByRole("heading", { name: "今天要处理什么？" })).toBeVisible();
    expect(requestJson).toHaveBeenCalledWith("/api/v1/projects/project-1/activate", {
      method: "POST",
      requireLease: true,
    });
  });

  it("reactivates a project every time navigation returns to it", async () => {
    let activeProjectId = "project-a";
    const requestJson = vi.fn(async (path: string, init?: { readonly method?: string }): Promise<unknown> => {
      if (path === "/api/v1/projects") return {
        projects: ["project-a", "project-b"].map((id) => ({
          active: id === activeProjectId,
          description: "",
          displayName: id === "project-a" ? "Project A" : "Project B",
          id,
          revision: "r1",
        })),
      };
      const activation = /^\/api\/v1\/projects\/(project-[ab])\/activate$/.exec(path);
      if (activation?.[1] && init?.method === "POST") {
        activeProjectId = activation[1];
        return { active: true, description: "", displayName: activeProjectId === "project-a" ? "Project A" : "Project B", id: activeProjectId, revision: "r1" };
      }
      const projectId = path.includes("projectId=project-b") ? "project-b" : "project-a";
      const sessionId = projectId === "project-b" ? "session-b" : "session-a";
      if (path.startsWith("/api/v1/conversations/messages?")) return { messages: [], projectId, sessionId };
      if (path.startsWith("/api/v1/conversations?")) return {
        activeSessionId: sessionId,
        conversations: [{ active: true, name: sessionId, preview: "", projectId, sessionId, timestamp: "now" }],
        projectId,
      };
      throw new Error(`unexpected request: ${path}`);
    });
    useConversationStore.getState().setActiveSession("project-a", "session-a");
    useConversationStore.getState().setActiveSession("project-b", "session-b");
    const gateway = { baseUrl: "https://api.example", requestJson } as unknown as ApiGateway;
    const user = userEvent.setup();
    render(<AppProviders>
      <MemoryRouter initialEntries={["/projects/project-a/conversations/session-a"]}>
        <AppRoutes gateway={gateway} />
        <LocationProbe />
      </MemoryRouter>
    </AppProviders>);
    await screen.findByRole("button", { name: "上传本地文件" });

    await user.click(screen.getByRole("link", { name: "Project B" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/project-b/conversations/session-b"));
    await screen.findByRole("button", { name: "上传本地文件" });
    await user.click(screen.getByRole("link", { name: "Project A" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/project-a/conversations/session-a"));
    await screen.findByRole("button", { name: "上传本地文件" });
    await user.click(screen.getByRole("link", { name: "Project B" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/project-b/conversations/session-b"));

    expect(requestJson.mock.calls.filter(([path]) => path === "/api/v1/projects/project-b/activate")).toHaveLength(2);
  });
});
