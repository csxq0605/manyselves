import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createWorkflowApi } from "./workflow-api";

describe("WorkflowApi", () => {
  it("uses the generic Run, Input, Output, Cost, cancel and resume paths", async () => {
    const requestJson = vi.fn().mockResolvedValue({});
    const api = createWorkflowApi({ requestJson } as unknown as ApiGateway);

    await api.provideInput("run 1", { values: { supplements: [] } }, "command-input");
    await api.outputs("run 1");
    await api.cost("run 1");
    await api.listRuns("conversation 1");
    await api.listRuns();
    await api.cancel("run 1");
    await api.resume("run 1", "command-resume");
    await api.resume("run 1", "command-recompute", true);

    expect(requestJson).toHaveBeenNthCalledWith(1, "/api/v1/runs/run%201/input", {
      headers: { "Idempotency-Key": "command-input" },
      json: { values: { supplements: [] } },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(2, "/api/v1/runs/run%201/outputs");
    expect(requestJson).toHaveBeenNthCalledWith(3, "/api/v1/runs/run%201/cost");
    expect(requestJson).toHaveBeenNthCalledWith(
      4,
      "/api/v1/runs?conversationId=conversation%201",
    );
    expect(requestJson).toHaveBeenNthCalledWith(5, "/api/v1/runs");
    expect(requestJson).toHaveBeenNthCalledWith(6, "/api/v1/runs/run%201/cancel", {
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(7, "/api/v1/runs/run%201/resume", {
      headers: { "Idempotency-Key": "command-resume" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(8, "/api/v1/runs/run%201/resume?recompute=true", {
      headers: { "Idempotency-Key": "command-recompute" },
      method: "POST",
      requireLease: true,
    });
  });
});
