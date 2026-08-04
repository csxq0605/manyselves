import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type Project = components["schemas"]["ProjectResponse"];
type ProjectList = components["schemas"]["ProjectListResponse"];
export type ProjectCreateInput = components["schemas"]["ProjectCreateRequest"];
export type ProjectUpdateInput = components["schemas"]["ProjectUpdateRequest"];

export interface ProjectApi {
  activate(projectId: string): Promise<Project>;
  create(input: ProjectCreateInput): Promise<Project>;
  delete(projectId: string): Promise<void>;
  list(): Promise<Project[]>;
  update(projectId: string, input: ProjectUpdateInput): Promise<Project>;
}

export function createProjectApi(gateway: ApiGateway): ProjectApi {
  return {
    activate: (projectId) =>
      gateway.requestJson<Project>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/activate`,
        { method: "POST", requireLease: true },
      ),
    create: (input) =>
      gateway.requestJson<Project>("/api/v1/projects", {
        json: input,
        method: "POST",
        requireLease: true,
      }),
    delete: (projectId) =>
      gateway.requestVoid(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
        method: "DELETE",
        requireLease: true,
      }),
    async list() {
      const response = await gateway.requestJson<ProjectList>("/api/v1/projects");
      return response.projects;
    },
    update: (projectId, input) =>
      gateway.requestJson<Project>(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
        json: input,
        method: "PATCH",
        requireLease: true,
      }),
  };
}
