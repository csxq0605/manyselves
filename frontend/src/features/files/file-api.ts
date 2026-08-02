import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type FileEntry = components["schemas"]["FileEntryResponse"];
type FileTreeResponse = components["schemas"]["FileTreeResponse"];
type CreateEntryRequest = components["schemas"]["CreateEntryRequest"];
type RenameEntryRequest = components["schemas"]["RenameEntryRequest"];

export interface FileApi {
  createEntry(projectId: string, input: CreateEntryRequest): Promise<FileEntry>;
  deleteEntry(projectId: string, path: string, baseRevision: string): Promise<void>;
  download(projectId: string, path: string): Promise<Blob>;
  listTree(projectId: string, path?: string): Promise<FileEntry[]>;
  renameEntry(projectId: string, input: RenameEntryRequest): Promise<FileEntry>;
  upload(projectId: string, path: string, file: File, signal?: AbortSignal): Promise<FileEntry>;
}

function projectFilesPath(projectId: string, suffix: string): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/files${suffix}`;
}

function withPath(path: string, value?: string): string {
  if (value === undefined) {
    return path;
  }
  const query = new URLSearchParams({ path: value });
  return `${path}?${query.toString()}`;
}

export function createFileApi(gateway: ApiGateway): FileApi {
  return {
    createEntry: (projectId, input) =>
      gateway.requestJson<FileEntry>(projectFilesPath(projectId, "/entries"), {
        json: input,
        method: "POST",
        requireLease: true,
      }),
    deleteEntry: (projectId, path, baseRevision) =>
      gateway.requestVoid(withPath(projectFilesPath(projectId, "/entries"), path), {
        headers: { "If-Match": baseRevision },
        method: "DELETE",
        requireLease: true,
      }),
    download: (projectId, path) =>
      gateway.requestBlob(withPath(projectFilesPath(projectId, "/download"), path)),
    async listTree(projectId, path) {
      const response = await gateway.requestJson<FileTreeResponse>(
        withPath(projectFilesPath(projectId, "/tree"), path),
      );
      return response.entries;
    },
    renameEntry: (projectId, input) =>
      gateway.requestJson<FileEntry>(projectFilesPath(projectId, "/rename"), {
        json: input,
        method: "POST",
        requireLease: true,
      }),
    upload: (projectId, path, file, signal) => {
      const request = { body: file, method: "POST", requireLease: true, ...(signal ? { signal } : {}) };
      return gateway.requestJson<FileEntry>(
        withPath(projectFilesPath(projectId, "/upload"), path),
        request,
      );
    },
  };
}
