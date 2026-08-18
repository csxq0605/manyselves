import { describe, expect, it, vi } from "vitest";

import { EventStream, type RuntimeEvent } from "./event-stream";

function runtimeEvent(
  eventId: string,
  type: string,
  sequence: number,
): RuntimeEvent {
  return {
    agentId: null,
    eventId,
    messageId: null,
    payload: {},
    projectId: "project-1",
    runId: null,
    schemaVersion: 1,
    sequence,
    sessionId: "session-1",
    streamId: "boot-a",
    timestamp: "2026-08-02T00:00:00Z",
    type,
  };
}

function sseResponse(events: readonly RuntimeEvent[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const event of events) {
        controller.enqueue(
          encoder.encode(
            `id: ${event.eventId}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`,
          ),
        );
      }
      controller.close();
    },
  });
  return new Response(body, {
    headers: { "Content-Type": "text/event-stream" },
    status: 200,
  });
}

describe("EventStream", () => {
  it("reconnects with the last event id and requests bootstrap on resync", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(sseResponse([runtimeEvent("boot-a:evt-9", "agent.status.changed", 9)]))
      .mockResolvedValueOnce(
        sseResponse([runtimeEvent("boot-a:evt-10", "stream.resync_required", 10)]),
      );
    const received: RuntimeEvent[] = [];
    const resync = vi.fn(async () => {
      stream.stop();
    });
    const stream = new EventStream({
      baseUrl: "https://server/",
      expectedStreamId: () => "boot-a",
      fetch: fetchMock,
      onEvent: (event) => received.push(event),
      onResync: resync,
      random: () => 0.5,
      sleep: async () => undefined,
    });

    await stream.start();

    expect(received.map((event) => event.eventId)).toEqual(["boot-a:evt-9"]);
    expect(resync).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const firstHeaders = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    const reconnectHeaders = new Headers(fetchMock.mock.calls[1]?.[1]?.headers);
    expect(fetchMock.mock.calls[0]?.[1]?.credentials).toBe("same-origin");
    expect(firstHeaders.has("Authorization")).toBe(false);
    expect(firstHeaders.get("Last-Event-ID")).toBeNull();
    expect(reconnectHeaders.get("Last-Event-ID")).toBe("boot-a:evt-9");
  });

  it("enters unauthorized state without reconnecting after HTTP 401", async () => {
    const states: string[] = [];
    const stream = new EventStream({
      baseUrl: "https://server",
      expectedStreamId: () => "boot-a",
      fetch: vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 401 })),
      onEvent: () => undefined,
      onResync: async () => undefined,
      onStateChange: (state) => states.push(state),
    });

    await stream.start();

    expect(states.at(-1)).toBe("unauthorized");
  });

  it("stops immediately while waiting for the reconnect delay", async () => {
    let markSleepReady: (() => void) | undefined;
    const sleepReady = new Promise<void>((resolve) => {
      markSleepReady = resolve;
    });
    let releaseSleep: (() => void) | undefined;
    const stream = new EventStream({
      baseUrl: "https://server",
      expectedStreamId: () => "boot-a",
      fetch: vi.fn<typeof fetch>().mockResolvedValue(sseResponse([])),
      onEvent: () => undefined,
      onResync: async () => undefined,
      sleep: async (_delay, signal) =>
        new Promise<void>((resolve) => {
          releaseSleep = resolve;
          signal.addEventListener("abort", () => resolve(), { once: true });
          markSleepReady?.();
        }),
    });

    const running = stream.start();
    await sleepReady;
    stream.stop();
    const outcome = await Promise.race([
      running.then(() => "stopped" as const),
      new Promise<"timeout">((resolve) => window.setTimeout(() => resolve("timeout"), 30)),
    ]);
    releaseSleep?.();
    await running;

    expect(outcome).toBe("stopped");
  });
});
