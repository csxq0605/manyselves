import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createOperationApi } from "./operation-api";

describe("OperationApi", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

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

  it("creates an idempotency key when insecure HTTP omits crypto.randomUUID", async () => {
    vi.stubGlobal("crypto", {
      getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto),
    });
    const requestJson = vi.fn().mockResolvedValue({ operationId: "op-1", status: "accepted" });
    const api = createOperationApi({ requestJson } as unknown as ApiGateway);

    await api.run("Scripts/a.py");

    expect(requestJson).toHaveBeenCalledWith("/api/v1/operations/python", {
      headers: {
        "Idempotency-Key": expect.stringMatching(
          /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
        ),
      },
      json: { arguments: [], path: "Scripts/a.py" },
      method: "POST",
      requireLease: true,
    });
  });
});
