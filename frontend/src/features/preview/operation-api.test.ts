import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createOperationApi } from "./operation-api";

describe("OperationApi", () => {
  it("requires the control lease and an idempotency key for run and interrupt", async () => {
    const requestJson = vi.fn().mockResolvedValue({ operationId: "op-1", status: "accepted" });
    const api = createOperationApi({ requestJson } as unknown as ApiGateway);

    await api.run("Scripts/a.py", ["--safe"]);
    await api.interrupt("op-1");

    expect(requestJson).toHaveBeenNthCalledWith(1, "/api/v1/operations/python", {
      headers: { "Idempotency-Key": expect.any(String) },
      json: { arguments: ["--safe"], path: "Scripts/a.py" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(2, "/api/v1/operations/op-1/interrupt", {
      method: "POST",
      requireLease: true,
    });
  });
});
