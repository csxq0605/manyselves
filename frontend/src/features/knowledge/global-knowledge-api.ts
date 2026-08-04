import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";
import type { UploadConflict } from "../files/file-api";

export type GlobalKnowledgeContent = components["schemas"]["GlobalKnowledgeFileContent"];
export type GlobalKnowledgeEntry = components["schemas"]["GlobalKnowledgeFileEntryResponse"];
export type GlobalKnowledgePreview = components["schemas"]["GlobalKnowledgePreviewResponse"];
type GlobalKnowledgeTree = components["schemas"]["GlobalKnowledgeFileTreeResponse"];

export interface GlobalKnowledgeApi {
  deleteEntry(path: string, baseRevision: string): Promise<void>;
  download(path: string): Promise<Blob>;
  downloadRange(url: string, begin: number, end: number): Promise<Uint8Array>;
  listTree(path?: string): Promise<GlobalKnowledgeEntry[]>;
  preview(path: string): Promise<GlobalKnowledgePreview>;
  read(path: string): Promise<GlobalKnowledgeContent>;
  save(path: string, content: string, baseRevision: string): Promise<GlobalKnowledgeContent>;
  upload(
    path: string,
    file: File,
    conflict: UploadConflict,
    baseRevision?: string,
    signal?: AbortSignal,
  ): Promise<GlobalKnowledgeEntry>;
}

const root = "/api/v1/global-knowledge/files";

function withPath(suffix: string, path: string): string {
  return `${root}${suffix}?${new URLSearchParams({ path }).toString()}`;
}

export function createGlobalKnowledgeApi(gateway: ApiGateway): GlobalKnowledgeApi {
  return {
    deleteEntry: (path, baseRevision) => gateway.requestVoid(withPath("/entries", path), {
      headers: { "If-Match": baseRevision },
      method: "DELETE",
      requireLease: true,
    }),
    download: (path) => gateway.requestBlob(withPath("/download", path)),
    async downloadRange(url, begin, end) {
      const blob = await gateway.requestBlob(url, {
        headers: { Range: `bytes=${begin}-${end - 1}` },
      });
      return new Uint8Array(await blob.arrayBuffer());
    },
    async listTree(path = "") {
      const response = await gateway.requestJson<GlobalKnowledgeTree>(withPath("/tree", path));
      return response.entries;
    },
    preview: (path) => gateway.requestJson<GlobalKnowledgePreview>(withPath("/preview", path)),
    read: (path) => gateway.requestJson<GlobalKnowledgeContent>(withPath("/content", path)),
    save: (path, content, baseRevision) => gateway.requestJson<GlobalKnowledgeContent>(
      withPath("/content", path),
      { json: { baseRevision, content }, method: "PUT", requireLease: true },
    ),
    upload: (path, file, conflict, baseRevision, signal) => {
      const query = new URLSearchParams({ conflict, path });
      if (baseRevision !== undefined) query.set("baseRevision", baseRevision);
      return gateway.requestJson<GlobalKnowledgeEntry>(`${root}/upload?${query.toString()}`, {
        body: file,
        method: "POST",
        requireLease: true,
        ...(signal ? { signal } : {}),
      });
    },
  };
}
