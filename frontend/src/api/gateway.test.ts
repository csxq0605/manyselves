import { describe, expect, it, vi } from "vitest";

import { ApiError, createApiGateway } from "./gateway";

const commandId = "00000000-0000-4000-8000-000000000001";

function gatewayWithResponse(response: Response) {
  return createApiGateway({
    baseUrl: "https://server/",
    clientId: "browser-client",
    fetch: vi.fn<typeof fetch>().mockResolvedValue(response),
    getLeaseToken: () => "lease-token",
  });
}

describe("ApiGateway", () => {
  it("loads bootstrap with the browser session cookie", async () => {
    const snapshot = {
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
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json(snapshot, { status: 200 }),
    );
    const gateway = createApiGateway({
      baseUrl: "https://server/",
      clientId: "browser-client",
      fetch: fetchMock,
      getLeaseToken: () => null,
    });

    await expect(gateway.bootstrap()).resolves.toEqual(snapshot);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    const headers = new Headers(init?.headers);
    expect(url).toBe("https://server/api/v1/bootstrap");
    expect(init?.credentials).toBe("same-origin");
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.get("X-Control-Lease-Token")).toBeNull();
  });

  it("sends the control lease and idempotency headers with browser credentials", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          commandId: "00000000-0000-4000-8000-000000000002",
          status: "accepted",
        }),
        {
          headers: { "Content-Type": "application/json" },
          status: 202,
        },
      ),
    );
    const gateway = createApiGateway({
      baseUrl: "https://server/",
      clientId: "browser-client",
      fetch: fetchMock,
      getLeaseToken: () => "lease-token",
    });

    await gateway.sendMessage(
      "main",
      { content: "hello", source: "user" },
      commandId,
    );

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    const headers = new Headers(init?.headers);
    expect(url).toBe("https://server/api/v1/agents/main/messages");
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("same-origin");
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.get("X-Control-Lease-Token")).toBe("lease-token");
    expect(headers.get("Idempotency-Key")).toBe(
      commandId,
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      content: "hello",
      source: "user",
    });
  });

  it("maps a structured non-success response into ApiError", async () => {
    const gateway = gatewayWithResponse(
      new Response(
        JSON.stringify({
          error: {
            code: "RUNTIME_BUSY",
            details: { operation: "report" },
            message: "Runtime is busy",
            retryable: true,
          },
          requestId: "request-42",
        }),
        {
          headers: { "Content-Type": "application/json" },
          status: 409,
        },
      ),
    );

    const result = gateway.sendMessage(
      "main",
      { content: "hello", source: "user" },
      commandId,
    );

    await expect(result).rejects.toEqual(
      expect.objectContaining({
        code: "RUNTIME_BUSY",
        details: { operation: "report" },
        message: "Runtime is busy",
        requestId: "request-42",
        retryable: true,
        status: 409,
      }),
    );
    await expect(result).rejects.toBeInstanceOf(ApiError);
  });

  it("maps a non-JSON failure without exposing its response body", async () => {
    const gateway = gatewayWithResponse(
      new Response("proxy diagnostic secret", {
        headers: { "Content-Type": "text/plain" },
        status: 502,
      }),
    );

    await expect(
      gateway.sendMessage(
        "main",
        { content: "hello", source: "user" },
        commandId,
      ),
    ).rejects.toEqual(
      expect.objectContaining({
        code: "HTTP_ERROR",
        details: {},
        message: "Request failed with HTTP 502",
        requestId: null,
        retryable: true,
        status: 502,
      }),
    );
  });

  it("sends JSON mutations through the authenticated transport", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json({ id: "project-2", active: true }),
    );
    const gateway = createApiGateway({
      baseUrl: "https://server/",
      clientId: "browser-client",
      fetch: fetchMock,
      getLeaseToken: () => "lease-token",
    });

    await gateway.requestJson("/api/v1/projects/project-2/activate", {
      json: {},
      method: "POST",
      requireLease: true,
    });

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    const headers = new Headers(init?.headers);
    expect(url).toBe("https://server/api/v1/projects/project-2/activate");
    expect(init?.body).toBe("{}");
    expect(init?.credentials).toBe("same-origin");
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-Control-Lease-Token")).toBe("lease-token");
  });

  it("returns binary downloads without parsing them as JSON", async () => {
    const gateway = gatewayWithResponse(new Response("report"));

    const downloaded = await gateway.requestBlob("/download");
    expect(await downloaded.text()).toBe("report");
  });

  it("accepts an empty successful response", async () => {
    const gateway = gatewayWithResponse(new Response(null, { status: 204 }));

    await expect(gateway.requestVoid("/entry", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("notifies the authentication boundary for every REST 401", async () => {
    const onUnauthorized = vi.fn();
    const gateway = createApiGateway({
      baseUrl: "https://server/",
      clientId: "browser-client",
      fetch: vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 401 })),
      getLeaseToken: () => null,
      onUnauthorized,
    });

    await expect(gateway.requestJson("/api/v1/projects")).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });
});
