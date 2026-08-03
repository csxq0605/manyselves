import type { Page, Route } from "@playwright/test";

export const runtimeSnapshot = {
  active_session_id: "session-1",
  agent_statuses: { main: "idle", researcher: "thinking" },
  checkpoints: [{
    agent_id: "researcher", checkpoint_id: "checkpoint-1", description: "Evidence collected",
    epoch: 3, message_id: "message-1", source: "agent", timestamp: "2026-08-03T08:00:00Z",
  }],
  controller_client_id: null,
  debug: [],
  queues: [{ agent_id: "researcher", pending_count: 1, queued_messages: ["Verify source"] }],
  ready: true,
  tasks: [{
    blocking: true, brief: "Review evidence", session_id: "session-1", source_agent: "main",
    status: "running", target_agent: "researcher", task_id: "task-1",
  }],
  tools: [{
    agentId: "researcher", arguments: { path: "Inputs/brief.md" }, error: null,
    name: "read_file", result: "ready", status: "completed", toolCallId: "tool-1",
  }],
  workspace: "project-1",
};

export const bootstrap = {
  agents: { main: "Main Agent", researcher: "Researcher" },
  conversations: [],
  maintenance: { quiesced: false },
  project: { id: "project-1" },
  runtime: runtimeSnapshot,
  settings: { control_lease_seconds: 30, sse_client_queue_capacity: 128, sse_replay_capacity: 512 },
  streamId: "stream-1",
};

export async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ body: JSON.stringify(body), contentType: "application/json", status });
}

export async function installBaseServer(
  page: Page,
  handler?: (route: Route, path: string) => Promise<boolean>,
): Promise<void> {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (handler && await handler(route, path)) return;
    if (path === "/api/v1/bootstrap") return json(route, bootstrap);
    if (path === "/api/v1/events") {
      return route.fulfill({ body: "", contentType: "text/event-stream", status: 200 });
    }
    if (path === "/api/v1/projects") {
      return json(route, { projects: [{ active: true, id: "project-1" }] });
    }
    if (path.endsWith("/files/tree")) return json(route, { entries: [] });
    if (path === "/api/v1/conversations") {
      return json(route, { activeSessionId: "session-1", conversations: [{
        active: true, agentId: "main", messageCount: 2, name: "需求梳理",
        sessionId: "session-1", updatedAt: "2026-08-03T08:00:00Z",
      }] });
    }
    if (path === "/api/v1/conversations/messages") {
      return json(route, { messages: [], sessionId: "session-1" });
    }
    return route.fulfill({ body: `Unhandled ${request.method()} ${path}`, status: 404 });
  });
}
