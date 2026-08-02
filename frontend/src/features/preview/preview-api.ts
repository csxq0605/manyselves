import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type PreviewResponse = components["schemas"]["PreviewResponse"];

export interface PreviewApi {
  download(url: string): Promise<Blob>;
  downloadRange(url: string, begin: number, end: number): Promise<Uint8Array>;
  get(projectId: string, path: string): Promise<PreviewResponse>;
}

export function createPreviewApi(gateway: ApiGateway): PreviewApi {
  return {
    download: (url) => gateway.requestBlob(url),
    async downloadRange(url, begin, end) {
      const blob = await gateway.requestBlob(url, {
        headers: { Range: `bytes=${begin}-${end - 1}` },
      });
      return new Uint8Array(await blob.arrayBuffer());
    },
    get: (projectId, path) => {
      const query = new URLSearchParams({ path });
      return gateway.requestJson<PreviewResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/files/preview?${query.toString()}`,
      );
    },
  };
}
