import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createGlobalKnowledgeApi } from "./global-knowledge-api";

describe("GlobalKnowledgeApi", () => {
  it("maps logical file operations without project ids or physical paths", async () => {
    const requestJson = vi.fn(async (path: string) => path.endsWith("/tree?path=") ? { entries: [] } : {});
    const requestVoid = vi.fn().mockResolvedValue(undefined);
    const requestBlob = vi.fn().mockResolvedValue(new Blob(["content"]));
    const api = createGlobalKnowledgeApi({ requestBlob, requestJson, requestVoid } as unknown as ApiGateway);
    const file = new File(["new"], "standard.md", { type: "text/markdown" });

    await api.listTree();
    await api.read("rules/standard.md");
    await api.preview("rules/standard.md");
    await api.save("rules/standard.md", "updated", "r1");
    await api.upload("standard.md", file, "replace", "r1");
    await api.download("rules/standard.md");
    await api.deleteEntry("rules/standard.md", "r1");

    expect(requestJson).toHaveBeenCalledWith("/api/v1/global-knowledge/files/tree?path=");
    expect(requestJson).toHaveBeenCalledWith("/api/v1/global-knowledge/files/content?path=rules%2Fstandard.md");
    expect(requestJson).toHaveBeenCalledWith("/api/v1/global-knowledge/files/preview?path=rules%2Fstandard.md");
    expect(requestJson).toHaveBeenCalledWith(
      "/api/v1/global-knowledge/files/content?path=rules%2Fstandard.md",
      { json: { baseRevision: "r1", content: "updated" }, method: "PUT", requireLease: true },
    );
    expect(requestJson).toHaveBeenCalledWith(
      "/api/v1/global-knowledge/files/upload?conflict=replace&path=standard.md&baseRevision=r1",
      { body: file, method: "POST", requireLease: true },
    );
    expect(requestBlob).toHaveBeenCalledWith("/api/v1/global-knowledge/files/download?path=rules%2Fstandard.md");
    expect(requestVoid).toHaveBeenCalledWith(
      "/api/v1/global-knowledge/files/entries?path=rules%2Fstandard.md",
      { headers: { "If-Match": "r1" }, method: "DELETE", requireLease: true },
    );
    expect(JSON.stringify([requestJson.mock.calls, requestVoid.mock.calls])).not.toContain(".manyselves");
  });
});
