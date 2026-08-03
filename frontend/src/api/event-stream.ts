import type { components } from "./generated/schema";
import type { ConnectionState } from "../store/connection-store";

export type RuntimeEvent = components["schemas"]["EventEnvelope"];

const reconnectDelays = [1_000, 2_000, 5_000, 10_000, 15_000] as const;

export interface EventStreamOptions {
  readonly baseUrl: string;
  readonly expectedStreamId: () => string | null;
  readonly fetch: typeof fetch;
  readonly onEvent: (event: RuntimeEvent) => void;
  readonly onResync: () => Promise<void>;
  readonly onStateChange?: (state: ConnectionState) => void;
  readonly random?: () => number;
  readonly sleep?: (delayMs: number, signal: AbortSignal) => Promise<void>;
}

function defaultSleep(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timeout = window.setTimeout(resolve, delayMs);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        resolve();
      },
      { once: true },
    );
  });
}

function isRuntimeEvent(value: unknown): value is RuntimeEvent {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const event = value as Partial<RuntimeEvent>;
  return (
    event.schemaVersion === 1 &&
    typeof event.streamId === "string" &&
    typeof event.eventId === "string" &&
    typeof event.sequence === "number" &&
    typeof event.type === "string" &&
    typeof event.timestamp === "string" &&
    typeof event.payload === "object" &&
    event.payload !== null
  );
}

async function* parseEventStream(body: ReadableStream<Uint8Array>) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const data = frame
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (!data) {
          continue;
        }
        let parsed: unknown;
        try {
          parsed = JSON.parse(data);
        } catch {
          continue;
        }
        if (isRuntimeEvent(parsed)) {
          yield parsed;
        }
      }
      if (done) {
        return;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export class EventStream {
  private abortController: AbortController | null = null;
  private lastEventId: string | null = null;
  private running = false;
  private stopped = false;

  constructor(private readonly options: EventStreamOptions) {}

  async start(): Promise<void> {
    if (this.running) {
      return;
    }
    this.running = true;
    this.stopped = false;
    let attempt = 0;
    this.options.onStateChange?.("connecting");
    try {
      while (!this.stopped) {
        const controller = new AbortController();
        this.abortController = controller;
        try {
          const headers = new Headers({ Accept: "text/event-stream" });
          if (this.lastEventId) {
            headers.set("Last-Event-ID", this.lastEventId);
          }
          const response = await this.options.fetch(
            `${this.options.baseUrl.replace(/\/+$/, "")}/api/v1/events`,
            { credentials: "same-origin", headers, signal: controller.signal },
          );
          if (response.status === 401) {
            this.options.onStateChange?.("unauthorized");
            return;
          }
          if (!response.ok || !response.body) {
            throw new Error(`Event stream failed with HTTP ${response.status}`);
          }

          attempt = 0;
          this.options.onStateChange?.("online");
          for await (const event of parseEventStream(response.body)) {
            if (this.stopped) {
              return;
            }
            const expectedStreamId = this.options.expectedStreamId();
            const validCursor = event.eventId.startsWith(`${event.streamId}:evt-`);
            if (
              event.type === "stream.resync_required" ||
              !expectedStreamId ||
              event.streamId !== expectedStreamId ||
              !validCursor
            ) {
              this.lastEventId = null;
              this.options.onStateChange?.("resyncing");
              await this.options.onResync();
              break;
            }
            this.lastEventId = event.eventId;
            this.options.onEvent(event);
          }
        } catch (error) {
          if (this.stopped || (error instanceof DOMException && error.name === "AbortError")) {
            return;
          }
          this.options.onStateChange?.("offline");
        }

        if (this.stopped) {
          return;
        }
        this.options.onStateChange?.("reconnecting");
        const baseDelay =
          reconnectDelays[Math.min(attempt, reconnectDelays.length - 1)] ?? 15_000;
        const jitter = 0.8 + (this.options.random?.() ?? Math.random()) * 0.4;
        attempt += 1;
        await (this.options.sleep ?? defaultSleep)(baseDelay * jitter, controller.signal);
        if (this.abortController === controller) {
          this.abortController = null;
        }
      }
    } finally {
      this.running = false;
      this.abortController = null;
    }
  }

  stop(): void {
    this.stopped = true;
    this.abortController?.abort();
  }
}
