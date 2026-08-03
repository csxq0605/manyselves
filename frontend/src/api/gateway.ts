import type { components } from "./generated/schema";

type AcceptedCommandResponse = components["schemas"]["AcceptedCommandResponse"];
type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];
type SendMessageRequest = components["schemas"]["SendMessageRequest"];
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
}

export interface ApiRequestOptions extends Omit<RequestInit, "body"> {
  readonly body?: BodyInit | null;
  readonly json?: unknown;
  readonly requireLease?: boolean;
}

export interface ApiGateway {
  readonly baseUrl: string;
  readonly clientId: string;
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

  async function sendRequest(
    path: string,
    init: ApiRequestOptions = {},
  ): Promise<Response> {
    const { json, requireLease, ...requestInit } = init;
    const headers = new Headers(requestInit.headers);
    if (requireLease) {
      const leaseToken = options.getLeaseToken();
      if (leaseToken) {
        headers.set("X-Control-Lease-Token", leaseToken);
      }
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

  const gateway: ApiGateway = {
    baseUrl,
    clientId: options.clientId,
    async bootstrap() {
      return gateway.requestJson<BootstrapSnapshot>("/api/v1/bootstrap");
    },
    async requestBlob(path, init) {
      return (await sendRequest(path, init)).blob();
    },
    async requestJson<T>(path: string, init?: ApiRequestOptions) {
      return (await sendRequest(path, init)).json() as Promise<T>;
    },
    async requestVoid(path, init) {
      await sendRequest(path, init);
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
