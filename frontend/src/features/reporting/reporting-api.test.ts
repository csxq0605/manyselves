import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createReportingApi } from "./reporting-api";

describe("ReportingApi", () => {
  it("uses named reporting resources and preserves caller idempotency keys", async () => {
    const requestJson = vi.fn().mockResolvedValue({
      commandId: "command-1",
      runId: "run-1",
      status: "accepted",
    });
    const gateway = { requestJson } as unknown as ApiGateway;
    const api = createReportingApi(gateway);

    await api.start({
      instruction: "生成供配电报告",
      maxProviderAttempts: 80,
      maxTotalTokens: 800_000,
      missingEvidencePolicy: "ask",
      operation: "full_report",
    }, "idempotency-1");
    await api.resume("run-1", { supplements: [] }, "idempotency-2");
    await api.decide("decision-1", { action: "draft" }, "idempotency-3");
    await api.cancel("run-1", "idempotency-4");

    expect(requestJson).toHaveBeenNthCalledWith(1, "/api/v1/reporting/runs", {
      headers: { "Idempotency-Key": "idempotency-1" },
      json: expect.objectContaining({ operation: "full_report" }),
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(2, "/api/v1/reporting/runs/run-1/resume", {
      headers: { "Idempotency-Key": "idempotency-2" },
      json: { supplements: [] },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(3, "/api/v1/reporting/decisions/decision-1/resume", {
      headers: { "Idempotency-Key": "idempotency-3" },
      json: { action: "draft" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(4, "/api/v1/reporting/runs/run-1/cancel", {
      headers: { "Idempotency-Key": "idempotency-4" },
      method: "POST",
      requireLease: true,
    });
  });

  it("downloads a verified artifact through the project-scoped file route", async () => {
    const blob = new Blob(["report"]);
    const requestBlob = vi.fn().mockResolvedValue(blob);
    const api = createReportingApi({ requestBlob } as unknown as ApiGateway);

    await expect(api.download("project 1", "Outputs/Reports/report.docx")).resolves.toBe(blob);
    expect(requestBlob).toHaveBeenCalledWith(
      "/api/v1/projects/project%201/files/download?path=Outputs%2FReports%2Freport.docx",
    );
  });
});
