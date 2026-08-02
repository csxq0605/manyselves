import { describe, expect, it, vi } from "vitest";

import { ApiError, createApiGateway } from "./gateway";

const commandId = "00000000-0000-4000-8000-000000000001";

function gatewayWithResponse(response: Response) {
  return createApiGateway({
    baseUrl: "https://server/",
    clientId: "browser-client",
    fetch: vi.fn<typeof fetch>().mockResolvedValue(response),
    getLeaseToken: () => "lease-token",
    getToken: () => "secret",
  });
}

describe("ApiGateway", () => {
  it("sends bearer, control lease, and idempotency headers", async () => {
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
      getToken: () => "secret",
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
    expect(headers.get("Authorization")).toBe("Bearer secret");
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
});
