import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type CapabilityListResponse = components["schemas"]["CapabilityListResponse"];
export type WorkflowCostResponse = components["schemas"]["WorkflowCostResponse"];
export type WorkflowInputSchemaResponse = components["schemas"]["WorkflowInputSchemaResponse"];
export type WorkflowListResponse = components["schemas"]["WorkflowListResponse"];
export type WorkflowOutputListResponse = components["schemas"]["WorkflowOutputListResponse"];
export type WorkflowRunAcceptedResponse = components["schemas"]["WorkflowRunAcceptedResponse"];
export type WorkflowRunListResponse = components["schemas"]["WorkflowRunListResponse"];
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
  cancel(runId: string): Promise<WorkflowRunResponse>;
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
  resume(runId: string, idempotencyKey: string): Promise<WorkflowRunAcceptedResponse>;
  start(
    input: WorkflowRunStartRequest,
    idempotencyKey: string,
  ): Promise<WorkflowRunAcceptedResponse>;
}

export interface WorkflowRunFeedApi {
  listRuns(conversationId?: string): Promise<WorkflowRunListResponse>;
}

function mutationOptions(idempotencyKey: string, json: unknown) {
  return {
    headers: { "Idempotency-Key": idempotencyKey },
    json,
    method: "POST",
    requireLease: true,
  } as const;
}

export function createWorkflowApi(gateway: ApiGateway): WorkflowApi & WorkflowRunFeedApi {
  return {
    cancel: (runId) => gateway.requestJson(
      `/api/v1/runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST", requireLease: true },
    ),
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
    listRuns: (conversationId) => gateway.requestJson(
      conversationId
        ? `/api/v1/runs?conversationId=${encodeURIComponent(conversationId)}`
        : "/api/v1/runs",
    ),
    listWorkflows: () => gateway.requestJson("/api/v1/workflows"),
    outputs: (runId) => gateway.requestJson(
      `/api/v1/runs/${encodeURIComponent(runId)}/outputs`,
    ),
    provideInput: (runId, input, idempotencyKey) => gateway.requestJson(
      `/api/v1/runs/${encodeURIComponent(runId)}/input`,
      mutationOptions(idempotencyKey, input),
    ),
    resume: (runId, idempotencyKey) => gateway.requestJson(
      `/api/v1/runs/${encodeURIComponent(runId)}/resume`,
      { headers: { "Idempotency-Key": idempotencyKey }, method: "POST", requireLease: true },
    ),
    start: (input, idempotencyKey) => gateway.requestJson(
      "/api/v1/runs",
      mutationOptions(idempotencyKey, input),
    ),
  };
}

/** One workflow, or one parallel branch of it, as the progress view shows it. */
export interface RunProgressNode {
  readonly current: string | null;
  readonly detail: RunProgressDetail | null;
  readonly status: string;
  readonly steps: readonly RunProgressStep[];
  readonly workflow_id: string;
}

export interface RunProgressStep {
  readonly id: string;
  readonly kind: string;
  readonly status: string;
  readonly workflow?: string;
}

export interface RunProgressMapItem {
  readonly key: string;
  readonly node: RunProgressNode | null;
  readonly status: string;
}

export type RunProgressDetail =
  | { readonly action_id: string; readonly child: RunProgressNode | null; readonly kind: "subworkflow" }
  | {
    readonly action_id: string;
    readonly completed: number;
    readonly completed_keys: readonly string[];
    readonly failed: number;
    readonly items: readonly RunProgressMapItem[];
    readonly kind: "map";
    readonly queued: number;
    readonly queued_keys: readonly string[];
    readonly running: number;
    readonly total: number;
    readonly workflow: string;
  }
  | {
    readonly action_id: string;
    readonly branches: readonly {
      readonly branch_id: string;
      readonly node: RunProgressNode | null;
      readonly start: string;
      readonly status: string;
    }[];
    readonly kind: "parallel";
  }
  | { readonly action_id: string; readonly index: number; readonly kind: "for_each"; readonly total: number };

export interface RunProgressEvent {
  readonly action_id: string | null;
  readonly agent_id: string | null;
  readonly error: string | null;
  readonly key: string | null;
  readonly kind: string;
  readonly reused: boolean | null;
  readonly tool_id: string | null;
  readonly ts: string | null;
  readonly workflow_id: string;
}

export interface RunProgressResponse {
  readonly events: readonly RunProgressEvent[];
  readonly root: RunProgressNode | null;
  readonly runId: string;
  readonly status: string;
  readonly workflowId: string;
}

export function fetchRunProgress(gateway: ApiGateway, runId: string): Promise<RunProgressResponse> {
  return gateway.requestJson(`/api/v1/runs/${encodeURIComponent(runId)}/progress`);
}
