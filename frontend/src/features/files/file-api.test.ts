import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createFileApi } from "./file-api";

describe("FileApi upload", () => {
  it("sends conflict mode and optional base revision through the frozen query contract", async () => {
    const file = new File(["new"], "data.csv", { type: "text/csv" });
    const requestJson = vi.fn().mockResolvedValue({
      kind: "file",
      modifiedAt: "2026-08-04T00:00:00Z",
      name: file.name,
      path: "Inputs/data.csv",
      revision: "a".repeat(64),
      size: file.size,
    });
    const api = createFileApi({ requestJson } as unknown as ApiGateway);

    await api.upload("project 1", "Inputs/data.csv", file, "replace", "revision 1");

    expect(requestJson).toHaveBeenCalledWith(
      "/api/v1/projects/project%201/files/upload?path=Inputs%2Fdata.csv&conflict=replace&baseRevision=revision+1",
      { body: file, method: "POST", requireLease: true },
    );
  });
});
