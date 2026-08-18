import type { components } from "./generated/schema";

type AcceptedCommandResponse = components["schemas"]["AcceptedCommandResponse"];
type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];
type LeaseResponse = components["schemas"]["LeaseResponse"];
type SendMessageRequest = components["schemas"]["SendMessageRequest"];
export type BootstrapSnapshot = components["schemas"]["BootstrapSnapshot"];

const heartbeatRetryDelayMs = 5_000;
const maximumHeartbeatDelayMs = 60_000;

export class ApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;
  readonly retryable: boolean;
  readonly status: number;

  constructor(input: {
    code: string;
    details: Record<string, unknown>;
    message: string;
    requestId: string | null;
    retryable: boolean;
    status: number;
  }) {
    super(input.message);
    this.name = "ApiError";
    this.code = input.code;
    this.details = input.details;
    this.requestId = input.requestId;
    this.retryable = input.retryable;
    this.status = input.status;
  }
}

export interface ApiGatewayOptions {
  readonly baseUrl: string;
  readonly clientId: string;
  readonly fetch: typeof fetch;
  readonly getLeaseToken: () => string | null;
  readonly onUnauthorized?: () => void;
  readonly setLeaseToken?: (token: string | null) => void;
}

export interface ApiRequestOptions extends Omit<RequestInit, "body"> {
  readonly body?: BodyInit | null;
  readonly json?: unknown;
  readonly requireLease?: boolean;
}

export interface ControlLeaseLifecycle {
  release(): Promise<void>;
  start(): void;
  stop(): void;
}

export interface ApiGateway {
  readonly baseUrl: string;
  readonly clientId: string;
  readonly controlLease: ControlLeaseLifecycle;
  bootstrap(): Promise<BootstrapSnapshot>;
  requestBlob(path: string, init?: ApiRequestOptions): Promise<Blob>;
  requestJson<T>(path: string, init?: ApiRequestOptions): Promise<T>;
  requestVoid(path: string, init?: ApiRequestOptions): Promise<void>;
  sendMessage(
    agentId: string,
    request: SendMessageRequest,
    idempotencyKey: string,
  ): Promise<AcceptedCommandResponse>;
}

interface LeaseVersion {
  readonly generation: number;
  readonly token: string;
}

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.replace(/\/+$/, "");
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const envelope = value as Partial<ErrorEnvelope>;
  const error = envelope.error;
  return (
    typeof envelope.requestId === "string"
    && typeof error === "object"
    && error !== null
    && typeof error.code === "string"
    && typeof error.message === "string"
    && typeof error.retryable === "boolean"
  );
}

async function toApiError(response: Response): Promise<ApiError> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (isErrorEnvelope(payload)) {
    return new ApiError({
      code: payload.error.code,
      details: payload.error.details ?? {},
      message: payload.error.message,
      requestId: payload.requestId,
      retryable: payload.error.retryable,
      status: response.status,
    });
  }
  return new ApiError({
    code: "HTTP_ERROR",
    details: {},
    message: `Request failed with HTTP ${response.status}`,
    requestId: null,
    retryable: response.status === 429 || response.status >= 500,
    status: response.status,
  });
}

export function createApiGateway(options: ApiGatewayOptions): ApiGateway {
  const baseUrl = normalizeBaseUrl(options.baseUrl);
  let acquisitionOrRenewal: Promise<void> | null = null;
  let heartbeatWork: Promise<void> | null = null;
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  let mounted = false;
  let disposed = false;
  let disposedLeaseToken: string | null = null;
  let releaseWork: Promise<void> | null = null;
  let generation = 0;
  let token = options.getLeaseToken();

  function currentLease(): LeaseVersion | null {
    return token ? { generation, token } : null;
  }

  function matches(lease: LeaseVersion): boolean {
    return generation === lease.generation && token === lease.token;
  }

  function clearHeartbeat(): void {
    if (heartbeatTimer !== null) {
      clearTimeout(heartbeatTimer);
      heartbeatTimer = null;
    }
  }
  function clearLease(expected?: LeaseVersion): boolean {
    if (expected && !matches(expected)) return false;
    token = null;
    generation += 1;
    options.setLeaseToken?.(null);
    clearHeartbeat();
    return true;
  }
  function trackLeaseWork(work: Promise<void>): Promise<void> {
    acquisitionOrRenewal = work;
    void work.then(
      () => {
        if (acquisitionOrRenewal === work) acquisitionOrRenewal = null;
      },
      () => {
        if (acquisitionOrRenewal === work) acquisitionOrRenewal = null;
      },
    );
    return work;
  }
  function trackHeartbeatWork(work: Promise<void>): Promise<void> {
    heartbeatWork = work;
    void work.then(
      () => {
        if (heartbeatWork === work) heartbeatWork = null;
      },
      () => {
        if (heartbeatWork === work) heartbeatWork = null;
      },
    );
    return work;
  }

  async function sendRequest(
    path: string,
    init: ApiRequestOptions = {},
    leaseToken?: string,
  ): Promise<Response> {
    const { json, requireLease, ...requestInit } = init;
    const headers = new Headers(requestInit.headers);
    if (requireLease && leaseToken) {
      headers.set("X-Control-Lease-Token", leaseToken);
    }
    let body = requestInit.body;
    if (json !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(json);
    }
    const request: RequestInit = { ...requestInit, credentials: "same-origin", headers };
    if (body !== undefined) {
      request.body = body;
    }
    const response = await options.fetch(`${baseUrl}${path}`, request);
    if (!response.ok) {
      const error = await toApiError(response);
      if (error.status === 401) {
        options.onUnauthorized?.();
      }
      throw error;
    }
    return response;
  }

  function scheduleHeartbeat(lease: LeaseVersion, expiresAt: string): void {
    clearHeartbeat();
    const expires = Date.parse(expiresAt);
    if (
      !mounted
      || disposed
      || !matches(lease)
      || !Number.isFinite(expires)
      || expires <= Date.now()
    ) {
      return;
    }
    const delay = Math.min(maximumHeartbeatDelayMs, Math.max(1_000, Math.floor((expires - Date.now()) / 2)));
    heartbeatTimer = setTimeout(() => {
      void heartbeat(lease);
    }, delay);
  }

  function scheduleHeartbeatRetry(lease: LeaseVersion): void {
    clearHeartbeat();
    if (!mounted || disposed || !matches(lease)) return;
    heartbeatTimer = setTimeout(() => {
      void heartbeat(lease);
    }, heartbeatRetryDelayMs);
  }

  function saveLease(response: LeaseResponse, source: LeaseVersion | null): void {
    if (disposed) {
      disposedLeaseToken = response.leaseToken;
      return;
    }
    if (source && !matches(source)) return;
    token = response.leaseToken;
    generation += 1;
    options.setLeaseToken?.(token);
    const updated = currentLease();
    if (updated) {
      scheduleHeartbeat(updated, response.expiresAt);
    }
  }

  async function acquireLease(): Promise<void> {
    if (disposed) throw new Error("Control lease lifecycle is closed");
    if (acquisitionOrRenewal) return acquisitionOrRenewal;
    if (token) return;
    const sourceGeneration = generation;
    const work = (async () => {
      const response = await sendRequest("/api/v1/control/lease", {
        json: { clientId: options.clientId, leaseToken: null },
        method: "POST",
      });
      const acquired = await response.json() as LeaseResponse;
      if (disposed) {
        saveLease(acquired, null);
        return;
      }
      if (generation !== sourceGeneration || token !== null) return;
      saveLease(acquired, null);
    })();
    return trackLeaseWork(work);
  }

  async function renewStoredLease(): Promise<void> {
    if (disposed || acquisitionOrRenewal) return acquisitionOrRenewal ?? Promise.resolve();
    const source = currentLease();
    if (!source) return;
    const work = (async () => {
      try {
        const response = await sendRequest("/api/v1/control/lease", {
          json: { clientId: options.clientId, leaseToken: source.token },
          method: "POST",
        });
        saveLease(await response.json() as LeaseResponse, source);
      } catch (error) {
        if (error instanceof ApiError && error.code === "CONTROL_LEASE_REQUIRED") {
          clearLease(source);
        }
      }
    })();
    return trackLeaseWork(work);
  }

  async function ensureLease(): Promise<LeaseVersion> {
    if (disposed) throw new Error("Control lease lifecycle is closed");
    if (acquisitionOrRenewal) await acquisitionOrRenewal;
    if (disposed) throw new Error("Control lease lifecycle is closed");
    let lease = currentLease();
    if (!lease) {
      await acquireLease();
      if (disposed) throw new Error("Control lease lifecycle is closed");
      lease = currentLease();
    }
    if (!lease) throw new Error("Control lease acquisition did not return a token");
    return lease;
  }

  async function heartbeat(lease: LeaseVersion): Promise<void> {
    if (!mounted || disposed || !matches(lease)) return;
    const work = (async () => {
      try {
        const response = await sendRequest("/api/v1/control/lease/heartbeat", {
          json: { leaseToken: lease.token },
          method: "POST",
        });
        saveLease(await response.json() as LeaseResponse, lease);
      } catch (error) {
        if (error instanceof ApiError && error.code === "CONTROL_LEASE_REQUIRED") {
          clearLease(lease);
        } else {
          scheduleHeartbeatRetry(lease);
        }
      }
    })();
    await trackHeartbeatWork(work);
  }

  async function request(path: string, init: ApiRequestOptions = {}, retried = false): Promise<Response> {
    const lease = init.requireLease ? await ensureLease() : null;
    try {
      return await sendRequest(path, init, lease?.token);
    } catch (error) {
      if (
        init.requireLease
        && lease
        && !retried
        && error instanceof ApiError
        && error.code === "CONTROL_LEASE_REQUIRED"
      ) {
        if (disposed) throw error;
        clearLease(lease);
        if (disposed) throw error;
        return request(path, init, true);
      }
      throw error;
    }
  }

  const controlLease: ControlLeaseLifecycle = {
    start() {
      mounted = true;
      void renewStoredLease();
    },
    stop() {
      mounted = false;
      clearHeartbeat();
    },
    async release() {
      if (releaseWork) return releaseWork;
      disposed = true;
      mounted = false;
      clearHeartbeat();
      const work = (async () => {
        await Promise.allSettled(
          [acquisitionOrRenewal, heartbeatWork]
            .filter((candidate): candidate is Promise<void> => candidate !== null),
        );
        const finalLease = currentLease();
        const finalToken = disposedLeaseToken ?? finalLease?.token;
        try {
          if (finalToken) {
            await sendRequest("/api/v1/control/lease", {
              json: { leaseToken: finalToken },
              method: "DELETE",
            });
          }
        } catch {
          // The server TTL handles a process or network failure during logout.
        } finally {
          disposedLeaseToken = null;
          clearLease();
        }
      })();
      releaseWork = work;
      return work;
    },
  };

  const gateway: ApiGateway = {
    baseUrl,
    clientId: options.clientId,
    controlLease,
    async bootstrap() {
      return gateway.requestJson<BootstrapSnapshot>("/api/v1/bootstrap");
    },
    async requestBlob(path, init) {
      return (await request(path, init)).blob();
    },
    async requestJson<T>(path: string, init?: ApiRequestOptions) {
      return (await request(path, init)).json() as Promise<T>;
    },
    async requestVoid(path, init) {
      await request(path, init);
    },
    async sendMessage(agentId, requestBody, idempotencyKey) {
      return gateway.requestJson<AcceptedCommandResponse>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/messages`,
        {
          headers: new Headers({ "Idempotency-Key": idempotencyKey }),
          json: requestBody,
          method: "POST",
          requireLease: true,
        },
      );
    },
  };
  return gateway;
}
