import { render, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { EventStreamOptions } from "../api/event-stream";
import type { ApiGateway, BootstrapSnapshot } from "../api/gateway";
import { createReportingStore } from "../features/reporting/reporting-store";
import { App } from "./App";
import { AppProviders } from "./providers";

const bootstrapSnapshot: BootstrapSnapshot = {
  agents: { main: "Main Agent" },
  conversations: [],
  maintenance: {},
  project: { id: "project-1" },
  runtime: { active_session_id: "session-1", agent_statuses: { main: "idle" }, checkpoints: [], controller_client_id: null, debug: [], queues: [], ready: true, tasks: [], tools: [], workspace: null },
  settings: { control_lease_seconds: 30, sse_client_queue_capacity: 128, sse_replay_capacity: 512 },
  streamId: "boot-a",
};

function renderApp(node: ReactNode, queryClient?: QueryClient) {
  return render(<AppProviders {...(queryClient ? { queryClient } : {})}><MemoryRouter>{node}</MemoryRouter></AppProviders>);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

describe("App event projection", () => {
  it("starts SSE before mounting route pages", async () => {
    const calls: string[] = [];
    const start = vi.fn(async () => { calls.push("sse"); });
    const requestJson = vi.fn().mockImplementation(async () => {
      calls.push("route");
      return { entries: [] };
    });

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/knowledge"]}>
          <App
            createEventStream={() => ({ start, stop: vi.fn() })}
            gateway={{ bootstrap: vi.fn().mockResolvedValue(bootstrapSnapshot), requestJson } as unknown as ApiGateway}
          />
        </MemoryRouter>
      </AppProviders>,
    );

    await waitFor(() => expect(requestJson).toHaveBeenCalledOnce());

    expect(calls).toEqual(["sse", "route"]);
  });

  it("waits for bootstrap before mounting route pages", async () => {
    const pendingBootstrap = deferred<BootstrapSnapshot>();
    const bootstrap = vi.fn(() => pendingBootstrap.promise);
    const requestJson = vi.fn().mockResolvedValue({ entries: [] });

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/knowledge"]}>
          <App gateway={{ bootstrap, requestJson } as unknown as ApiGateway} />
        </MemoryRouter>
      </AppProviders>,
    );

    await waitFor(() => expect(bootstrap).toHaveBeenCalledOnce());
    expect(requestJson).not.toHaveBeenCalled();

    pendingBootstrap.resolve(bootstrapSnapshot);

    await waitFor(() => expect(requestJson).toHaveBeenCalledOnce());
  });

  it("refreshes bootstrap when the event stream requires resync", async () => {
    const bootstrap = vi.fn().mockResolvedValue(bootstrapSnapshot);
    const gateway = { bootstrap, sendMessage: vi.fn() } as unknown as ApiGateway;
    let eventStreamOptions: EventStreamOptions | undefined;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const streamFetch = vi.fn<typeof fetch>();

    renderApp(<App eventSource={{ baseUrl: "https://api.example/", fetch: streamFetch }} gateway={gateway} createEventStream={(options) => { eventStreamOptions = options; return { start: async () => undefined, stop: () => undefined }; }} />, queryClient);

    await waitFor(() => expect(eventStreamOptions).toBeDefined());
    expect(eventStreamOptions?.baseUrl).toBe("https://api.example/");
    expect(eventStreamOptions?.fetch).toBe(streamFetch);
    expect(queryClient.getQueryData(["project"])).toEqual(bootstrapSnapshot.project);
    await eventStreamOptions?.onResync();
    expect(bootstrap).toHaveBeenCalledTimes(2);
  });

  it("refreshes bootstrap on a real sequence gap", async () => {
    const bootstrap = vi.fn().mockResolvedValue(bootstrapSnapshot);
    const gateway = { bootstrap } as unknown as ApiGateway;
    let eventStreamOptions: EventStreamOptions | undefined;

    renderApp(<App gateway={gateway} createEventStream={(options) => { eventStreamOptions = options; return { start: async () => undefined, stop: () => undefined }; }} />);
    await waitFor(() => expect(eventStreamOptions).toBeDefined());
    eventStreamOptions?.onEvent({ agentId: "Researcher", eventId: "boot-a:evt-10", payload: { agent_type: "Researcher", status: "running" }, schemaVersion: 1, sequence: 10, streamId: "boot-a", timestamp: "2026-08-03T08:00:00Z", type: "agent.status.changed" });
    eventStreamOptions?.onEvent({ eventId: "boot-a:evt-12", payload: { content: "gap" }, schemaVersion: 1, sequence: 12, streamId: "boot-a", timestamp: "2026-08-03T08:00:01Z", type: "system.notice" });

    await waitFor(() => expect(bootstrap).toHaveBeenCalledTimes(2));
  });

  it("projects reporting SSE events into the hydrated reporting snapshot", async () => {
    const bootstrap = vi.fn().mockResolvedValue(bootstrapSnapshot);
    const gateway = { baseUrl: "https://api.example", bootstrap } as unknown as ApiGateway;
    const reportingStore = createReportingStore("boot-a");
    reportingStore.getState().hydrateSnapshot("run-1", { checkpoint: { activity: "dispatch", specialist_modules: [], status: "in_progress" }, evidence: {}, outputs: [], revision: {}, run: { active: true, run_id: "run-1", status: "running" }, state: { activity: "dispatch", specialist_modules: [], status: "in_progress" }, waitingInput: [] });
    let eventStreamOptions: EventStreamOptions | undefined;

    renderApp(<App gateway={gateway} reportingStore={reportingStore} createEventStream={(options) => { eventStreamOptions = options; return { start: async () => undefined, stop: () => undefined }; }} />);
    await waitFor(() => expect(eventStreamOptions).toBeDefined());
    eventStreamOptions?.onEvent({ eventId: "boot-a:evt-7", payload: { result_path: "Work/runs/run-1/results/module-2.4.json", run_id: "run-1", sender: "module-2.4-specialist", status: "completed", task_id: "module-2.4" }, schemaVersion: 1, sequence: 7, streamId: "boot-a", timestamp: "2026-08-03T08:00:07Z", type: "reporting.agent_result.changed" });

    await waitFor(() => expect(reportingStore.getState().snapshots["run-1"]?.agents["module-2.4-specialist"]?.status).toBe("completed"));
  });
});
