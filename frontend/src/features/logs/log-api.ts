import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type EventLogEntry = components["schemas"]["EventLogEntry"];
export type EventLogLevel = EventLogEntry["level"];
export type EventLogResponse = components["schemas"]["EventLogResponse"];

export interface LogApi {
  list(projectId: string, limit?: number): Promise<EventLogResponse>;
}

export function createLogApi(gateway: ApiGateway): LogApi {
  return {
    list(projectId, limit = 200) {
      const query = new URLSearchParams({ projectId, limit: String(limit) });
      return gateway.requestJson<EventLogResponse>(`/api/v1/events/logs?${query.toString()}`);
    },
  };
}
