import type { components } from "./generated/schema";

type AcceptedCommandResponse = components["schemas"]["AcceptedCommandResponse"];
type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];
type SendMessageRequest = components["schemas"]["SendMessageRequest"];

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
  readonly getToken: () => string | null;
}

export interface ApiGateway {
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

  return {
    async sendMessage(agentId, request, idempotencyKey) {
      const headers = new Headers({
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      });
      const token = options.getToken();
      if (token) {
        headers.set("Authorization", `Bearer ${token}`);
      }
      const leaseToken = options.getLeaseToken();
      if (leaseToken) {
        headers.set("X-Control-Lease-Token", leaseToken);
      }

      const response = await options.fetch(
        `${baseUrl}/api/v1/agents/${encodeURIComponent(agentId)}/messages`,
        {
          body: JSON.stringify(request),
          headers,
          method: "POST",
        },
      );
      if (!response.ok) {
        throw await toApiError(response);
      }
      return (await response.json()) as AcceptedCommandResponse;
    },
  };
}
