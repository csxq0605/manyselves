import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type Project = components["schemas"]["ProjectResponse"];
type ProjectList = components["schemas"]["ProjectListResponse"];

export interface ProjectApi {
  activate(projectId: string): Promise<Project>;
  create(projectId: string): Promise<Project>;
  list(): Promise<Project[]>;
}

export function createProjectApi(gateway: ApiGateway): ProjectApi {
  return {
    activate: (projectId) =>
      gateway.requestJson<Project>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/activate`,
        { method: "POST", requireLease: true },
      ),
    create: (projectId) =>
      gateway.requestJson<Project>("/api/v1/projects", {
        json: { projectId },
        method: "POST",
        requireLease: true,
      }),
    async list() {
      const response = await gateway.requestJson<ProjectList>("/api/v1/projects");
      return response.projects;
    },
  };
}
