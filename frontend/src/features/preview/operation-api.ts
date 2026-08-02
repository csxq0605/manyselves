import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type OperationAccepted = components["schemas"]["OperationAcceptedResponse"];
export type PythonOperation = components["schemas"]["PythonOperationResponse"];

export interface OperationApi {
  get(operationId: string): Promise<PythonOperation>;
  interrupt(operationId: string): Promise<PythonOperation>;
  run(path: string, arguments_?: readonly string[]): Promise<OperationAccepted>;
}

export function createOperationApi(gateway: ApiGateway): OperationApi {
  return {
    get: (operationId) => gateway.requestJson<PythonOperation>(
      `/api/v1/operations/${encodeURIComponent(operationId)}`,
    ),
    interrupt: (operationId) => gateway.requestJson<PythonOperation>(
      `/api/v1/operations/${encodeURIComponent(operationId)}/interrupt`,
      { method: "POST", requireLease: true },
    ),
    run: (path, arguments_ = []) => gateway.requestJson<OperationAccepted>(
      "/api/v1/operations/python",
      {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        json: { arguments: [...arguments_], path },
        method: "POST",
        requireLease: true,
      },
    ),
  };
}
