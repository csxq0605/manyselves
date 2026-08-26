import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { useRunStore } from "../../store/run-store";
import { ConversationWorkspace } from "./ConversationWorkspace";

describe("ConversationWorkspace", () => {
  it("passes runtime snapshot summaries into the inline agent panel", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return {
          messages: [
            { content: "read", role: "tool_call", timestamp: "2026-08-05T22:20:12" },
            { content: "server history", message_id: "m1", role: "agent" },
          ],
          projectId: "project-1",
          sessionId: "s1",
        };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "s1",
          conversations: [{ active: true, name: "runtime", preview: "server", projectId: "project-1", sessionId: "s1", timestamp: "now" }],
          projectId: "project-1",
        };
      }
      if (path.includes("/files/tree")) return { entries: [] };
      throw new Error(`unexpected path: ${path}`);
    });
    const gateway = {
      bootstrap: async () => ({
        project: { active: true, description: "", displayName: "Project 1", id: "project-1", revision: "r1" },
        runtime: {
          active_session_id: "s1",
          agent_statuses: { main: "running", researcher: "waiting" },
          checkpoints: [],
          controller_client_id: null,
          debug: [],
          queues: [],
          ready: true,
          tasks: [
            { blocking: false, brief: "Build report", session_id: "s1", source_agent: "main", status: "running", target_agent: "researcher", task_id: "task-1" },
            { blocking: false, brief: "Done task", session_id: "s1", source_agent: "main", status: "completed", target_agent: "main", task_id: "task-2" },
          ],
          tools: [{ agentId: "researcher", arguments: {}, name: "read_file", status: "running", toolCallId: "tool-1" }],
          workspace: null,
        },
      }),
      requestJson,
    } as unknown as ApiGateway;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={gateway} projectId="project-1" />
    </QueryClientProvider>);

    const toggle = await screen.findByRole("button", { name: "\u5c55\u5f00 Agent \u8fd0\u884c\u6001" });
    expect(toggle).toHaveTextContent("1 \u8fdb\u884c\u4e2d");
    expect(toggle).toHaveTextContent("1/2 \u5b8c\u6210");

    await user.click(toggle);

    expect(screen.getByText("Build report")).toBeVisible();
    expect(screen.getByText("\u670d\u52a1\u5c31\u7eea")).toBeVisible();
    expect(screen.getByText("1 \u4e2a\u5de5\u5177\u8fd0\u884c\u4e2d")).toBeVisible();
  });

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

  it("resumes a waiting lane through three schema-defined choices in the Main conversation", async () => {
    const user = userEvent.setup();
    let waiting = true;
    const requestJson = vi.fn(async (path: string, init?: { readonly json?: unknown; readonly method?: string }) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return { messages: [], projectId: "project-1", sessionId: "s1" };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "s1",
          conversations: [{ active: true, name: "Main", preview: "", projectId: "project-1", sessionId: "s1", timestamp: "now" }],
          projectId: "project-1",
        };
      }
      if (path === "/api/v1/runs?conversationId=s1") {
        return {
          runs: waiting ? [{
            run: { active: false, capabilityId: "distribution-reporting", runId: "full-report-1", status: "waiting", taskId: null, workflowId: "full-report" },
            state: { status: "waiting" },
            waitingInput: [{
              contract_id: "declarative_main_exception_user_input",
              description: "请选择如何处理 2.4 模块的审查争议。",
              input_id: "request-main-exception-decision",
              path: [{ action_id: "run-module-lanes", branch_id: "2.4", kind: "parallel" }],
              schema: {
                properties: {
                  decision: {
                    enum: ["accept_dispute", "return_to_author", "stop_incomplete"],
                    title: "处理方式",
                    type: "string",
                    "x-enum-labels": {
                      accept_dispute: "接受争议并继续",
                      return_to_author: "退回作者修改",
                      stop_incomplete: "停止并保留不完整结果",
                    },
                  },
                  rationale: { title: "说明", type: "string" },
                },
                required: ["decision", "rationale"],
                type: "object",
              },
              title: "需要你的决定",
            }],
          }] : [],
        };
      }
      if (path === "/api/v1/runs/full-report-1/input" && init?.method === "POST") {
        expect(init.json).toEqual({
          inputId: "request-main-exception-decision",
          values: { decision: "return_to_author", rationale: "补充 2.4 的证据说明。" },
        });
        waiting = false;
        return { capabilityId: "distribution-reporting", commandId: "command-1", runId: "full-report-1", status: "accepted", taskId: null, workflowId: "full-report" };
      }
      if (path.includes("/files/tree")) return { entries: [] };
      throw new Error(`unexpected path: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={{ requestJson } as unknown as ApiGateway} projectId="project-1" />
    </QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "需要你的决定" })).toBeVisible();
    expect(screen.getByText("模块 2.4")).toBeVisible();
    await user.click(screen.getByRole("radio", { name: "退回作者修改" }));
    await user.type(screen.getByRole("textbox", { name: "说明" }), "补充 2.4 的证据说明。");
    await user.click(screen.getByRole("button", { name: "提交并继续原运行" }));

    await waitFor(() => expect(
      requestJson.mock.calls.some(([path]) => path === "/api/v1/runs/full-report-1/input"),
    ).toBe(true));
  });

  it("resumes an interrupted original run from the Main conversation", async () => {
    const user = userEvent.setup();
    const requestJson = vi.fn(async (path: string, init?: { readonly method?: string }) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return { messages: [], projectId: "project-1", sessionId: "s1" };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "s1",
          conversations: [{ active: true, name: "Main", preview: "", projectId: "project-1", sessionId: "s1", timestamp: "now" }],
          projectId: "project-1",
        };
      }
      if (path === "/api/v1/runs?conversationId=s1") return {
        runs: [{
          run: { active: false, capabilityId: "distribution-reporting", runId: "full-report-2", status: "failed", taskId: null, workflowId: "full-report" },
          state: {
            error: "source='public-reporting:module-2.1-specialist' message='网络连接失败，已自动重试2次仍失败；服务端接收状态未确认：Connection error.' details={'attempt_disposition': 'accepted_or_unknown'}",
            error_action_id: "run-module-cohort",
            status: "failed",
          },
          waitingInput: [],
        }],
      };
      if (path === "/api/v1/runs/full-report-2/resume" && init?.method === "POST") {
        return { capabilityId: "distribution-reporting", commandId: "command-2", runId: "full-report-2", status: "accepted", taskId: null, workflowId: "full-report" };
      }
      if (path.includes("/files/tree")) return { entries: [] };
      throw new Error(`unexpected path: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={{ requestJson } as unknown as ApiGateway} projectId="project-1" />
    </QueryClientProvider>);

    expect(await screen.findByText("配电安全报告失败")).toBeVisible();
    expect(screen.getByText("失败阶段 · run-module-cohort")).toBeVisible();
    expect(screen.getByText("网络连接失败，已自动重试2次仍失败；服务端接收状态未确认：Connection error.")).toBeVisible();
    expect(screen.queryByText("配电安全报告运行中")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "恢复原报告" }));
    await waitFor(() => expect(requestJson.mock.calls.some(
      ([path]) => path === "/api/v1/runs/full-report-2/resume",
    )).toBe(true));
  });

  it("shows an active original run in the Main conversation", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return { messages: [], projectId: "project-1", sessionId: "s1" };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "s1",
          conversations: [{ active: true, name: "Main", preview: "", projectId: "project-1", sessionId: "s1", timestamp: "now" }],
          projectId: "project-1",
        };
      }
      if (path === "/api/v1/runs?conversationId=s1") return {
        runs: [{
          run: { active: true, capabilityId: "distribution-reporting", runId: "full-report-3", status: "running", taskId: null, workflowId: "full-report" },
          state: { status: "running" },
          waitingInput: [],
        }],
      };
      if (path.includes("/files/tree")) return { entries: [] };
      throw new Error(`unexpected path: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={{ requestJson } as unknown as ApiGateway} projectId="project-1" />
    </QueryClientProvider>);

    expect(await screen.findByText("配电安全报告运行中")).toBeVisible();
    expect(screen.getByText("full-report · full-report-3")).toBeVisible();
    expect(screen.queryByRole("link", { name: "查看运行" })).not.toBeInTheDocument();
  });

  it("does not inject a remembered project run into an unrelated Main conversation", async () => {
    useRunStore.getState().setCurrentRun("project-1", "full-report-current");
    const requestJson = vi.fn(async (path: string) => {
      if (path.startsWith("/api/v1/conversations/messages")) {
        return { messages: [], projectId: "project-1", sessionId: "s1" };
      }
      if (path.startsWith("/api/v1/conversations?")) {
        return {
          activeSessionId: "s1",
          conversations: [{ active: true, name: "Main", preview: "", projectId: "project-1", sessionId: "s1", timestamp: "now" }],
          projectId: "project-1",
        };
      }
      if (path === "/api/v1/runs?conversationId=s1") throw new Error("historical run is unbound");
      if (path.includes("/files/tree")) return { entries: [] };
      throw new Error(`unexpected path: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}>
      <ConversationWorkspace agentId="main" gateway={{ requestJson } as unknown as ApiGateway} projectId="project-1" />
    </QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "今天要处理什么？" })).toBeVisible();
    await waitFor(() => expect(requestJson).toHaveBeenCalledWith(
      "/api/v1/runs?conversationId=s1",
    ));
    expect(screen.queryByText("运行中")).not.toBeInTheDocument();
    expect(requestJson).not.toHaveBeenCalledWith("/api/v1/runs/full-report-current");
    useRunStore.getState().setCurrentRun("project-1", null);
  });
});
