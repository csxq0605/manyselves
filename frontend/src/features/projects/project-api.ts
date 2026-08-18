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

const activationQueues = new WeakMap<ApiGateway, Promise<void>>();

function activateProject(gateway: ApiGateway, projectId: string): Promise<Project> {
  const previous = activationQueues.get(gateway) ?? Promise.resolve();
  const activation = previous.then(() => gateway.requestJson<Project>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/activate`,
    { method: "POST", requireLease: true },
  ));
  const tail = activation.then(() => undefined, () => undefined);
  activationQueues.set(gateway, tail);
  void tail.then(() => {
    if (activationQueues.get(gateway) === tail) activationQueues.delete(gateway);
  });
  return activation;
}

export function createProjectApi(gateway: ApiGateway): ProjectApi {
  return {
    activate: (projectId) => activateProject(gateway, projectId),
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
