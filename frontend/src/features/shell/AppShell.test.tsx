import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";

import { App } from "../../app/App";
import { AppProviders } from "../../app/providers";
import type { EventStreamOptions } from "../../api/event-stream";
import type { ApiGateway, BootstrapSnapshot } from "../../api/gateway";
import { AppShell } from "./AppShell";

const bootstrapSnapshot: BootstrapSnapshot = {
  agents: { main: "Main Agent" },
  conversations: [],
  maintenance: {},
  project: { id: "project-1" },
  runtime: {
    active_session_id: "session-1",
    agent_statuses: { main: "idle" },
    checkpoints: [],
    controller_client_id: null,
    debug: [],
    queues: [],
    ready: true,
    tasks: [],
    tools: [],
    workspace: null,
  },
  settings: {
    control_lease_seconds: 30,
    sse_client_queue_capacity: 128,
    sse_replay_capacity: 512,
  },
  streamId: "boot-a",
};

describe("AppShell", () => {
  it("shows offline state without discarding local drafts", () => {
    render(
      <AppProviders
        initialConnectionState="offline"
        initialDrafts={{ "Inputs/notes.md": "unsaved" }}
      >
        <AppShell />
      </AppProviders>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("连接已中断");
    expect(screen.getByRole("textbox", { name: "本地草稿" })).toHaveValue("unsaved");
    expect(screen.getByRole("navigation", { name: "服务器工作区" })).toBeVisible();
    expect(screen.getByRole("main", { name: "主工作区" })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "智能体与控制" })).toBeVisible();
  });

  it("refreshes bootstrap when the event stream requires resync", async () => {
    const bootstrap = vi.fn().mockResolvedValue(bootstrapSnapshot);
    const gateway = {
      bootstrap,
      sendMessage: vi.fn(),
    } as unknown as ApiGateway;
    let eventStreamOptions: EventStreamOptions | undefined;
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const streamFetch = vi.fn<typeof fetch>();

    render(
      <AppProviders queryClient={queryClient}>
        <App
          eventSource={{
            baseUrl: "https://api.example/",
            fetch: streamFetch,
            getToken: () => "stream-token",
          }}
          gateway={gateway}
          createEventStream={(options) => {
            eventStreamOptions = options;
            return { start: async () => undefined, stop: () => undefined };
          }}
        />
      </AppProviders>,
    );

    expect(await screen.findByText("project-1")).toBeVisible();
    await waitFor(() => expect(eventStreamOptions).toBeDefined());
    expect(eventStreamOptions?.baseUrl).toBe("https://api.example/");
    expect(eventStreamOptions?.fetch).toBe(streamFetch);
    expect(eventStreamOptions?.getToken()).toBe("stream-token");
    expect(queryClient.getQueryData(["project"])).toEqual(bootstrapSnapshot.project);
    expect(queryClient.getQueryData(["runtime"])).toEqual(bootstrapSnapshot.runtime);
    expect(queryClient.getQueryData(["agents"])).toEqual(bootstrapSnapshot.agents);
    await eventStreamOptions?.onResync();

    expect(bootstrap).toHaveBeenCalledTimes(2);
  });

  it("projects SSE runtime events and refreshes bootstrap on a real sequence gap", async () => {
    const bootstrap = vi.fn().mockResolvedValue(bootstrapSnapshot);
    const gateway = { bootstrap } as unknown as ApiGateway;
    let eventStreamOptions: EventStreamOptions | undefined;

    render(
      <AppProviders>
        <App
          gateway={gateway}
          createEventStream={(options) => {
            eventStreamOptions = options;
            return { start: async () => undefined, stop: () => undefined };
          }}
        />
      </AppProviders>,
    );

    expect(await screen.findByText("project-1")).toBeVisible();
    await waitFor(() => expect(eventStreamOptions).toBeDefined());
    eventStreamOptions?.onEvent({
      agentId: "Researcher",
      eventId: "boot-a:evt-10",
      payload: { agent_type: "Researcher", status: "running" },
      schemaVersion: 1,
      sequence: 10,
      streamId: "boot-a",
      timestamp: "2026-08-03T08:00:00Z",
      type: "agent.status.changed",
    });

    expect(await screen.findByText("researcher")).toBeVisible();
    eventStreamOptions?.onEvent({
      eventId: "boot-a:evt-12",
      payload: { content: "gap" },
      schemaVersion: 1,
      sequence: 12,
      streamId: "boot-a",
      timestamp: "2026-08-03T08:00:01Z",
      type: "system.notice",
    });

    await waitFor(() => expect(bootstrap).toHaveBeenCalledTimes(2));
  });
});
