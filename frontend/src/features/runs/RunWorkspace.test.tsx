import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/providers";
import { RunWorkspace } from "./RunWorkspace";
import type { WorkflowApi } from "./workflow-api";

describe("RunWorkspace", () => {
  it("starts a generic workflow and reads Run, Output, and Cost projections", async () => {
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "run-1",
        usage: { totals: { estimated_cost: 1.25, total_tokens: 15 } },
      }),
      get: vi.fn().mockResolvedValue({
        run: {
          active: false,
          capabilityId: "distribution-reporting",
          runId: "run-1",
          status: "completed",
          taskId: "task-1",
          workflowId: "distribution-reporting",
        },
        state: {},
        waitingInput: [],
      }),
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "distribution_reporting_input",
        schema: { properties: { instruction: { type: "string" } }, type: "object" },
        workflowId: "distribution-reporting",
      }),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [{
          description: "Declarative distribution reporting capability",
          id: "distribution-reporting",
          version: "1.0.0",
          workflowIds: ["distribution-reporting"],
        }],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [{
          capabilityId: "distribution-reporting",
          description: "Distribution reporting",
          id: "distribution-reporting",
          inputContract: "distribution_reporting_input",
          outputContract: "distribution_reporting_output",
          runnable: true,
          version: "1.0.0",
        }],
      }),
      outputs: vi.fn().mockResolvedValue({
        outputs: [{ exists: true, path: "Outputs/Reports/report.docx", size: 4 }],
        runId: "run-1",
      }),
      provideInput: vi.fn(),
      start: vi.fn().mockResolvedValue({
        capabilityId: "distribution-reporting",
        commandId: "command-1",
        runId: "run-1",
        status: "accepted",
        taskId: "task-1",
        workflowId: "distribution-reporting",
      }),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);

    expect(await screen.findByRole("heading", { name: "通用工作流" })).toBeVisible();
    expect(await screen.findByText("Declarative distribution reporting capability")).toBeVisible();
    const input = screen.getByRole("textbox", { name: "运行输入 JSON" });
    await user.clear(input);
    await user.click(input);
    await user.paste('{"instruction":"Generate report"}');
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith(
      { input: { instruction: "Generate report" }, workflowId: "distribution-reporting" },
      expect.any(String),
    ));
    expect(await screen.findByText("completed")).toBeVisible();
    expect(screen.getByText("Outputs/Reports/report.docx")).toBeVisible();
    expect(screen.getByText("15 tokens")).toBeVisible();
    expect(screen.getByText("1.25")).toBeVisible();
    expect(screen.queryByText(/sha/i)).not.toBeInTheDocument();
  });
});
