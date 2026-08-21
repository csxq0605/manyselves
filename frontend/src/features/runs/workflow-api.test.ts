import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createWorkflowApi } from "./workflow-api";

describe("WorkflowApi", () => {
  it("uses every generic Capability, Workflow, Run, Output, Cost, and Event path", async () => {
    const requestJson = vi.fn().mockResolvedValue({});
    const api = createWorkflowApi({ requestJson } as unknown as ApiGateway);

    await api.listCapabilities();
    await api.listWorkflows();
    await api.inputSchema("distribution-reporting");
    await api.start(
      { input: { instruction: "Generate report" }, workflowId: "distribution-reporting" },
      "command-start",
    );
    await api.get("run 1");
    await api.provideInput("run 1", { values: { supplements: [] } }, "command-input");
    await api.outputs("run 1");
    await api.cost("run 1");
    await api.events!("run 1");

    expect(requestJson).toHaveBeenNthCalledWith(1, "/api/v1/capabilities");
    expect(requestJson).toHaveBeenNthCalledWith(2, "/api/v1/workflows");
    expect(requestJson).toHaveBeenNthCalledWith(
      3,
      "/api/v1/workflows/distribution-reporting/input-schema",
    );
    expect(requestJson).toHaveBeenNthCalledWith(4, "/api/v1/runs", {
      headers: { "Idempotency-Key": "command-start" },
      json: { input: { instruction: "Generate report" }, workflowId: "distribution-reporting" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(5, "/api/v1/runs/run%201");
    expect(requestJson).toHaveBeenNthCalledWith(6, "/api/v1/runs/run%201/input", {
      headers: { "Idempotency-Key": "command-input" },
      json: { values: { supplements: [] } },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(7, "/api/v1/runs/run%201/outputs");
    expect(requestJson).toHaveBeenNthCalledWith(8, "/api/v1/runs/run%201/cost");
    expect(requestJson).toHaveBeenNthCalledWith(9, "/api/v1/runs/run%201/events");
  });
});
