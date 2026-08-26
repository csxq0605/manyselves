import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../api/gateway";
import { AppRoutes } from "./routes";
import { AppProviders } from "./providers";

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function NavigationProbe() {
  const navigate = useNavigate();
  return <>
    <button onClick={() => navigate("/projects/project-1/conversations/new")} type="button">新建项目一会话</button>
    <button onClick={() => navigate("/projects/project-2/conversations/new")} type="button">新建项目二会话</button>
  </>;
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

describe("generic workflow route", () => {
  it("mounts the project-scoped Capability and Workflow projection", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" }],
      };
      if (path === "/api/v1/capabilities") return {
        capabilities: [{ description: "Distribution Reporting", id: "distribution-reporting", version: "1.0.0", workflowIds: ["distribution-reporting"] }],
      };
      if (path === "/api/v1/workflows") return {
        workflows: [{ capabilityId: "distribution-reporting", description: "Full report", id: "distribution-reporting", inputContract: "distribution_reporting_input", outputContract: "distribution_reporting_output", runnable: true, version: "1.0.0" }],
      };
      if (path === "/api/v1/workflows/distribution-reporting/input-schema") return {
        contractId: "distribution_reporting_input", schema: { type: "object" }, workflowId: "distribution-reporting",
      };
      throw new Error(`unexpected request: ${path}`);
    });

    render(<AppProviders><MemoryRouter initialEntries={["/projects/project-1/workflows"]}><AppRoutes gateway={{ requestJson } as unknown as ApiGateway} /></MemoryRouter></AppProviders>);

    expect(await screen.findByRole("heading", { name: "通用工作流" }, { timeout: 10_000 })).toBeVisible();
    expect(await screen.findByText("Distribution Reporting")).toBeVisible();
    await waitFor(() => expect(requestJson).toHaveBeenCalledWith(
      "/api/v1/workflows/distribution-reporting/input-schema",
    ));
  }, 15_000);

  it("activates an inactive route project before loading workflow definitions", async () => {
    let projectActive = false;
    const requestJson = vi.fn(async (path: string, init?: { readonly method?: string }) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: projectActive, description: "", displayName: "Project 2", id: "project-2", revision: "r1" }],
      };
      if (path === "/api/v1/projects/project-2/activate" && init?.method === "POST") {
        projectActive = true;
        return { active: true, description: "", displayName: "Project 2", id: "project-2", revision: "r2" };
      }
      if (!projectActive) throw new Error(`workflow definitions loaded before activation: ${path}`);
      if (path === "/api/v1/capabilities") return {
        capabilities: [{ description: "Neutral Parameter Adjustment", id: "parameter-adjustment", version: "1.0.0", workflowIds: ["parameter-adjustment"] }],
      };
      if (path === "/api/v1/workflows") return {
        workflows: [{ capabilityId: "parameter-adjustment", description: "Parameter adjustment", id: "parameter-adjustment", inputContract: "parameter-input", outputContract: "parameter-value", runnable: true, version: "1.0.0" }],
      };
      if (path === "/api/v1/workflows/parameter-adjustment/input-schema") return {
        contractId: "parameter-input", schema: { type: "object" }, workflowId: "parameter-adjustment",
      };
      throw new Error(`unexpected request: ${path}`);
    });

    render(<AppProviders><MemoryRouter initialEntries={["/projects/project-2/workflows"]}><AppRoutes gateway={{ requestJson } as unknown as ApiGateway} /></MemoryRouter></AppProviders>);

    expect(await screen.findByRole("heading", { name: "通用工作流" }, { timeout: 10_000 })).toBeVisible();
    expect(await screen.findByText("Neutral Parameter Adjustment")).toBeVisible();
    expect(requestJson).toHaveBeenCalledWith("/api/v1/projects/project-2/activate", {
      method: "POST",
      requireLease: true,
    });
    const paths = requestJson.mock.calls.map(([path]) => path);
    expect(paths.indexOf("/api/v1/projects/project-2/activate")).toBeLessThan(
      paths.indexOf("/api/v1/capabilities"),
    );
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

    await user.click(screen.getByRole("link", { name: "新对话" }));
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

  it("publishes a newly created conversation before navigating away from the new route", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(["conversations", "project-1", "main"], {
      activeSessionId: "s-old",
      conversations: [{
        active: true,
        name: "旧会话",
        preview: "",
        projectId: "project-1",
        sessionId: "s-old",
        timestamp: "before",
      }],
      projectId: "project-1",
    });
    const requestJson = vi.fn(async (path: string, init?: { readonly method?: string }) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" }],
      };
      if (path === "/api/v1/conversations" && init?.method === "POST") return {
        active: true,
        name: "新会话",
        preview: "",
        projectId: "project-1",
        sessionId: "s-new",
        timestamp: "now",
      };
      if (path === "/api/v1/conversations?projectId=project-1&agentId=main") {
        return await new Promise(() => undefined);
      }
      if (path === "/api/v1/conversations/messages?projectId=project-1&agentId=main") {
        return { messages: [], projectId: "project-1", sessionId: "s-new" };
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(
      <AppProviders queryClient={queryClient}>
        <MemoryRouter initialEntries={["/projects/project-1/conversations/new"]}>
          <AppRoutes gateway={{ baseUrl: "https://api.example", requestJson } as unknown as ApiGateway} />
          <LocationProbe />
        </MemoryRouter>
      </AppProviders>,
    );

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/conversations/s-new",
    ));
    expect(screen.queryByText("该会话不属于当前项目")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "今天要处理什么？" }, { timeout: 1_000 })).toBeVisible();
  });

  it("creates a fresh conversation whenever the same project re-enters the new route", async () => {
    let createCount = 0;
    const requestJson = vi.fn(async (path: string, init?: { readonly json?: { readonly projectId?: string }; readonly method?: string }) => {
      if (path === "/api/v1/projects") return {
        projects: [{ active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" }],
      };
      if (path === "/api/v1/projects/project-1/activate" && init?.method === "POST") {
        return { active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" };
      }
      if (path === "/api/v1/conversations" && init?.method === "POST") {
        createCount += 1;
        return {
          active: true,
          name: "新会话",
          preview: "",
          projectId: init.json?.projectId ?? "project-1",
          sessionId: `s-new-${createCount}`,
          timestamp: "now",
        };
      }
      if (path.startsWith("/api/v1/conversations?")) return {
        activeSessionId: `s-new-${createCount}`,
        conversations: [],
        projectId: "project-1",
      };
      if (path.startsWith("/api/v1/conversations/messages?")) return {
        messages: [], projectId: "project-1", sessionId: `s-new-${createCount}`,
      };
      throw new Error(`unexpected request: ${path}`);
    });
    const user = userEvent.setup();

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/projects/project-1/conversations/new"]}>
          <AppRoutes gateway={{ baseUrl: "https://api.example", requestJson } as unknown as ApiGateway} />
          <NavigationProbe />
          <LocationProbe />
        </MemoryRouter>
      </AppProviders>,
    );

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/conversations/s-new-1",
    ));
    await user.click(screen.getByRole("button", { name: "新建项目一会话" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/conversations/s-new-2",
    ));
    expect(requestJson.mock.calls.filter(([path]) => path === "/api/v1/conversations")).toHaveLength(2);
  });

  it("reactivates a previously visited project before creating another conversation", async () => {
    let activeProject = "project-2";
    const creates = new Map<string, number>();
    const requestJson = vi.fn(async (path: string, init?: { readonly json?: { readonly projectId?: string }; readonly method?: string }) => {
      if (path === "/api/v1/projects") return {
        projects: ["project-1", "project-2"].map((projectId) => ({
          active: activeProject === projectId,
          description: "",
          displayName: projectId,
          id: projectId,
          revision: "r1",
        })),
      };
      const activation = path.match(/^\/api\/v1\/projects\/(project-[12])\/activate$/);
      if (activation && init?.method === "POST") {
        activeProject = activation[1]!;
        return {
          active: true,
          description: "",
          displayName: activeProject,
          id: activeProject,
          revision: "r1",
        };
      }
      if (path === "/api/v1/conversations" && init?.method === "POST") {
        const projectId = init.json?.projectId ?? "";
        if (projectId !== activeProject) throw new Error(`inactive project: ${projectId}`);
        const count = (creates.get(projectId) ?? 0) + 1;
        creates.set(projectId, count);
        return {
          active: true,
          name: "新会话",
          preview: "",
          projectId,
          sessionId: `${projectId}-s${count}`,
          timestamp: "now",
        };
      }
      if (path.startsWith("/api/v1/conversations?")) return {
        activeSessionId: "",
        conversations: [],
        projectId: activeProject,
      };
      if (path.startsWith("/api/v1/conversations/messages?")) return {
        messages: [], projectId: activeProject, sessionId: "",
      };
      throw new Error(`unexpected request: ${path}`);
    });
    const user = userEvent.setup();

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/projects/project-1/conversations/new"]}>
          <AppRoutes gateway={{ baseUrl: "https://api.example", requestJson } as unknown as ApiGateway} />
          <NavigationProbe />
          <LocationProbe />
        </MemoryRouter>
      </AppProviders>,
    );

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/conversations/project-1-s1",
    ));
    await user.click(screen.getByRole("button", { name: "新建项目二会话" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-2/conversations/project-2-s1",
    ));
    await user.click(screen.getByRole("button", { name: "新建项目一会话" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/conversations/project-1-s2",
    ));
    expect(requestJson.mock.calls.filter(([path]) => path === "/api/v1/projects/project-1/activate")).toHaveLength(2);
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
});
