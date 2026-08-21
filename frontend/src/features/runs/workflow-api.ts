import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type CapabilityListResponse = components["schemas"]["CapabilityListResponse"];
export type WorkflowCostResponse = components["schemas"]["WorkflowCostResponse"];
export type WorkflowInputSchemaResponse = components["schemas"]["WorkflowInputSchemaResponse"];
export type WorkflowListResponse = components["schemas"]["WorkflowListResponse"];
export type WorkflowOutputListResponse = components["schemas"]["WorkflowOutputListResponse"];
export type WorkflowRunAcceptedResponse = components["schemas"]["WorkflowRunAcceptedResponse"];
export type WorkflowEvent = components["schemas"]["WorkflowEvent"];
export type WorkflowEventsResponse = components["schemas"]["WorkflowEventListResponse"];
type GeneratedWorkflowRunInputRequest = components["schemas"]["WorkflowRunInputRequest"];
type GeneratedWorkflowRunStartRequest = components["schemas"]["WorkflowRunStartRequest"];
export type WorkflowRunInputRequest = Omit<GeneratedWorkflowRunInputRequest, "values"> & {
  readonly values?: unknown;
};
export type WorkflowRunResponse = components["schemas"]["WorkflowRunResponse"];
export type WorkflowRunStartRequest = Omit<GeneratedWorkflowRunStartRequest, "input"> & {
  readonly input: unknown;
};

export interface WorkflowApi {
  cost(runId: string): Promise<WorkflowCostResponse>;
  events?(runId: string): Promise<WorkflowEventsResponse>;
  get(runId: string): Promise<WorkflowRunResponse>;
  inputSchema(workflowId: string): Promise<WorkflowInputSchemaResponse>;
  listCapabilities(): Promise<CapabilityListResponse>;
  listWorkflows(): Promise<WorkflowListResponse>;
  outputs(runId: string): Promise<WorkflowOutputListResponse>;
  provideInput(
    runId: string,
    input: WorkflowRunInputRequest,
    idempotencyKey: string,
  ): Promise<WorkflowRunAcceptedResponse>;
  start(
    input: WorkflowRunStartRequest,
    idempotencyKey: string,
  ): Promise<WorkflowRunAcceptedResponse>;
}

function mutationOptions(idempotencyKey: string, json: unknown) {
  return {
    headers: { "Idempotency-Key": idempotencyKey },
    json,
    method: "POST",
    requireLease: true,
  } as const;
}

export function createWorkflowApi(gateway: ApiGateway): WorkflowApi {
  return {
    cost: (runId) => gateway.requestJson(
      `/api/v1/runs/${encodeURIComponent(runId)}/cost`,
    ),
    events: (runId) => gateway.requestJson(
      `/api/v1/runs/${encodeURIComponent(runId)}/events`,
    ),
    get: (runId) => gateway.requestJson(`/api/v1/runs/${encodeURIComponent(runId)}`),
    inputSchema: (workflowId) => gateway.requestJson(
      `/api/v1/workflows/${encodeURIComponent(workflowId)}/input-schema`,
    ),
    listCapabilities: () => gateway.requestJson("/api/v1/capabilities"),
    listWorkflows: () => gateway.requestJson("/api/v1/workflows"),
    outputs: (runId) => gateway.requestJson(
      `/api/v1/runs/${encodeURIComponent(runId)}/outputs`,
    ),
    provideInput: (runId, input, idempotencyKey) => gateway.requestJson(
      `/api/v1/runs/${encodeURIComponent(runId)}/input`,
      mutationOptions(idempotencyKey, input),
    ),
    start: (input, idempotencyKey) => gateway.requestJson(
      "/api/v1/runs",
      mutationOptions(idempotencyKey, input),
    ),
  };
}
