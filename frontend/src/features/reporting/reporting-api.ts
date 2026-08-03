import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type ReportingAcceptedResponse = components["schemas"]["ReportingAcceptedResponse"];
export type ReportingDecisionRequest = components["schemas"]["ReportingDecisionRequest"];
export type ReportingListResponse = components["schemas"]["ReportingListResponse"];
export type ReportingResumeRequest = components["schemas"]["ReportingResumeRequest"];
export type ReportingRevisionRequest = components["schemas"]["ReportingRevisionRequest"];
export type ReportingSnapshotResponse = components["schemas"]["ReportingSnapshotResponse"];
export type ReportingStartRequest = components["schemas"]["ReportingStartRequest"];
export type UserSupplement = components["schemas"]["UserSupplement"];

export interface ReportingApi {
  cancel(runId: string, idempotencyKey: string): Promise<ReportingAcceptedResponse>;
  decide(
    decisionId: string,
    input: ReportingDecisionRequest,
    idempotencyKey: string,
  ): Promise<ReportingAcceptedResponse>;
  download(projectId: string, path: string): Promise<Blob>;
  get(runId: string): Promise<ReportingSnapshotResponse>;
  list(): Promise<ReportingListResponse>;
  resume(
    runId: string,
    input: ReportingResumeRequest,
    idempotencyKey: string,
  ): Promise<ReportingAcceptedResponse>;
  revise(
    input: ReportingRevisionRequest,
    idempotencyKey: string,
  ): Promise<ReportingAcceptedResponse>;
  start(
    input: ReportingStartRequest,
    idempotencyKey: string,
  ): Promise<ReportingAcceptedResponse>;
}

function mutationOptions(idempotencyKey: string, json?: unknown) {
  return {
    headers: { "Idempotency-Key": idempotencyKey },
    ...(json === undefined ? {} : { json }),
    method: "POST",
    requireLease: true,
  } as const;
}

export function createReportingApi(gateway: ApiGateway): ReportingApi {
  return {
    cancel: (runId, idempotencyKey) => gateway.requestJson<ReportingAcceptedResponse>(
      `/api/v1/reporting/runs/${encodeURIComponent(runId)}/cancel`,
      mutationOptions(idempotencyKey),
    ),
    decide: (decisionId, input, idempotencyKey) =>
      gateway.requestJson<ReportingAcceptedResponse>(
        `/api/v1/reporting/decisions/${encodeURIComponent(decisionId)}/resume`,
        mutationOptions(idempotencyKey, input),
      ),
    download: (projectId, path) => {
      const query = new URLSearchParams({ path });
      return gateway.requestBlob(
        `/api/v1/projects/${encodeURIComponent(projectId)}/files/download?${query.toString()}`,
      );
    },
    get: (runId) => gateway.requestJson<ReportingSnapshotResponse>(
      `/api/v1/reporting/runs/${encodeURIComponent(runId)}`,
    ),
    list: () => gateway.requestJson<ReportingListResponse>("/api/v1/reporting/runs"),
    resume: (runId, input, idempotencyKey) =>
      gateway.requestJson<ReportingAcceptedResponse>(
        `/api/v1/reporting/runs/${encodeURIComponent(runId)}/resume`,
        mutationOptions(idempotencyKey, input),
      ),
    revise: (input, idempotencyKey) => gateway.requestJson<ReportingAcceptedResponse>(
      "/api/v1/reporting/revisions",
      mutationOptions(idempotencyKey, input),
    ),
    start: (input, idempotencyKey) => gateway.requestJson<ReportingAcceptedResponse>(
      "/api/v1/reporting/runs",
      mutationOptions(idempotencyKey, input),
    ),
  };
}
