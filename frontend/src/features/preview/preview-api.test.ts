import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createPreviewApi } from "./preview-api";

describe("PreviewApi", () => {
  it("loads authenticated PDF byte ranges through the gateway", async () => {
    const requestBlob = vi.fn().mockResolvedValue(new Blob([new Uint8Array([1, 2, 3])]));
    const api = createPreviewApi({ requestBlob } as unknown as ApiGateway);

    const bytes = await api.downloadRange("/api/v1/range.pdf", 10, 20);

    expect([...bytes]).toEqual([1, 2, 3]);
    expect(requestBlob).toHaveBeenCalledWith("/api/v1/range.pdf", {
      headers: { Range: "bytes=10-19" },
    });
  });
});
