import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type EventLogEntry = components["schemas"]["EventLogEntry"];
export type EventLogLevel = EventLogEntry["level"];

// Manually add total field since schema is outdated
export interface EventLogResponse {
  projectId: string;
  entries: EventLogEntry[];
  total: number;
}

export interface EventLogStats {
  total: number;
  byLevel: Record<string, number>;
  byType: Record<string, number>;
  dateRange: {
    start: string | null;
    end: string | null;
  };
}

export interface LogApi {
  list(projectId: string, limit?: number, offset?: number): Promise<EventLogResponse>;
  search(projectId: string, query: string, limit?: number): Promise<EventLogEntry[]>;
  stats(projectId: string): Promise<EventLogStats>;
}

export function createLogApi(gateway: ApiGateway): LogApi {
  return {
    list(projectId, limit = 200, offset = 0) {
      const query = new URLSearchParams({
        projectId,
        limit: String(limit),
        offset: String(offset),
      });
      return gateway.requestJson<EventLogResponse>(`/api/v1/events/logs?${query.toString()}`);
    },

    search(projectId, query, limit = 100) {
      const params = new URLSearchParams({
        projectId,
        q: query,
        limit: String(limit),
      });
      return gateway.requestJson<EventLogEntry[]>(`/api/v1/events/search?${params.toString()}`);
    },

    stats(projectId) {
      const params = new URLSearchParams({ projectId });
      return gateway.requestJson<EventLogStats>(`/api/v1/events/stats?${params.toString()}`);
    },
  };
}
