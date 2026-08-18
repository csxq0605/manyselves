import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type FileContent = components["schemas"]["FileContent"];

export interface EditorFileApi {
  read(projectId: string, path: string): Promise<FileContent>;
  save(
    projectId: string,
    path: string,
    content: string,
    baseRevision: string,
  ): Promise<FileContent>;
}

function contentPath(projectId: string, path: string): string {
  const query = new URLSearchParams({ path });
  return `/api/v1/projects/${encodeURIComponent(projectId)}/files/content?${query.toString()}`;
}

export function createEditorFileApi(gateway: ApiGateway): EditorFileApi {
  return {
    read: (projectId, path) => gateway.requestJson<FileContent>(contentPath(projectId, path)),
    save: (projectId, path, content, baseRevision) =>
      gateway.requestJson<FileContent>(contentPath(projectId, path), {
        json: { baseRevision, content },
        method: "PUT",
        requireLease: true,
      }),
  };
}
