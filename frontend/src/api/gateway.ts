import type { components } from "./generated/schema";

type AcceptedCommandResponse = components["schemas"]["AcceptedCommandResponse"];
type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];
type SendMessageRequest = components["schemas"]["SendMessageRequest"];
type LeaseResponse = components["schemas"]["LeaseResponse"];
export type BootstrapSnapshot = components["schemas"]["BootstrapSnapshot"];

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

export interface ControlLeaseLifecycle {
  release(): Promise<void>;
  start(): void;
  stop(): void;
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
    typeof envelope.requestId === "string" &&
    typeof error === "object" &&
    error !== null &&
    typeof error.code === "string" &&
    typeof error.message === "string" &&
    typeof error.retryable === "boolean"
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
  let acquisition: Promise<void> | null = null;
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  let mounted = false;
  let token = options.getLeaseToken();

  async function sendRequest(
    path: string,
    init: ApiRequestOptions = {},
  ): Promise<Response> {
    const { json, requireLease, ...requestInit } = init;
    const headers = new Headers(requestInit.headers);
    if (requireLease && token) {
      headers.set("X-Control-Lease-Token", token);
    }
    let body = requestInit.body;
    if (json !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(json);
    }
    const request: RequestInit = {
      ...requestInit,
      credentials: "same-origin",
      headers,
    };
    if (body !== undefined) {
      request.body = body;
    }
    const response = await options.fetch(`${baseUrl}${path}`, request);
    if (!response.ok) {
      const error = await toApiError(response);
      if (error.status === 401) options.onUnauthorized?.();
      throw error;
    }
    return response;
  }

  function clearHeartbeat(): void {
    if (heartbeatTimer !== null) {
      clearTimeout(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function saveLease(response: LeaseResponse): void {
    token = response.leaseToken;
    options.setLeaseToken?.(token);
    scheduleHeartbeat(response.expiresAt);
  }

  function clearLease(): void {
    token = null;
    options.setLeaseToken?.(null);
    clearHeartbeat();
  }

  function scheduleHeartbeat(expiresAt: string): void {
    clearHeartbeat();
    if (!mounted || !token) return;
    const expiresIn = Date.parse(expiresAt) - Date.now();
    const delay = Math.max(1_000, Math.floor(expiresIn / 2));
    heartbeatTimer = setTimeout(() => { void heartbeat(); }, delay);
  }

  async function acquireLease(): Promise<void> {
    if (token) return;
    if (acquisition) return acquisition;
    acquisition = (async () => {
      const response = await sendRequest("/api/v1/control/lease", {
        json: { clientId: options.clientId, leaseToken: token },
        method: "POST",
      });
      saveLease(await response.json() as LeaseResponse);
    })().finally(() => { acquisition = null; });
    return acquisition;
  }

  async function renewStoredLease(): Promise<void> {
    const proof = token;
    if (!proof || acquisition) return acquisition ?? Promise.resolve();
    acquisition = (async () => {
      try {
        const response = await sendRequest("/api/v1/control/lease", {
          json: { clientId: options.clientId, leaseToken: proof },
          method: "POST",
        });
        saveLease(await response.json() as LeaseResponse);
      } catch (error) {
        if (error instanceof ApiError && error.code === "CONTROL_LEASE_REQUIRED") clearLease();
      }
    })().finally(() => { acquisition = null; });
    return acquisition;
  }

  async function heartbeat(): Promise<void> {
    if (!mounted || !token) return;
    try {
      const response = await sendRequest("/api/v1/control/lease/heartbeat", {
        json: { leaseToken: token },
        method: "POST",
      });
      saveLease(await response.json() as LeaseResponse);
    } catch (error) {
      if (error instanceof ApiError && error.code === "CONTROL_LEASE_REQUIRED") clearLease();
    }
  }

  async function request(path: string, init: ApiRequestOptions = {}, retried = false): Promise<Response> {
    if (init.requireLease) await acquireLease();
    try {
      return await sendRequest(path, init);
    } catch (error) {
      if (
        init.requireLease
        && !retried
        && error instanceof ApiError
        && error.code === "CONTROL_LEASE_REQUIRED"
      ) {
        clearLease();
        await acquireLease();
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
    stop() { mounted = false; clearHeartbeat(); },
    async release() {
      const heldToken = token;
      controlLease.stop();
      try {
        if (heldToken) {
          await sendRequest("/api/v1/control/lease", {
            json: { leaseToken: heldToken },
            method: "DELETE",
          });
        }
      } catch {
        // Logout and unmount are allowed to rely on the server-side lease TTL.
      } finally {
        clearLease();
      }
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
    async sendMessage(agentId, request, idempotencyKey) {
      const headers = new Headers({
        "Idempotency-Key": idempotencyKey,
      });
      return gateway.requestJson<AcceptedCommandResponse>(
        `/api/v1/agents/${encodeURIComponent(agentId)}/messages`,
        {
          headers,
          json: request,
          method: "POST",
          requireLease: true,
        },
      );
    },
  };
  return gateway;
}
