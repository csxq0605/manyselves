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
        outputs: [{
          exists: true,
          id: "Outputs/Reports/report.docx",
          kind: "artifact",
          path: "Outputs/Reports/report.docx",
          size: 4,
        }],
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

  it("selects and displays value output from the second neutral capability", async () => {
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "parameter-adjustment-command-2",
        usage: { totals: { estimated_cost: 0, total_tokens: 0 } },
      }),
      get: vi.fn().mockResolvedValue({
        run: {
          active: false,
          capabilityId: "parameter-adjustment",
          runId: "parameter-adjustment-command-2",
          status: "completed",
          taskId: null,
          workflowId: "parameter-adjustment",
        },
        state: {},
        waitingInput: [],
      }),
      inputSchema: vi.fn().mockImplementation(async (workflowId: string) => ({
        contractId: workflowId === "parameter-adjustment"
          ? "parameter-input"
          : "distribution_reporting_input",
        schema: { type: "object" },
        workflowId,
      })),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [
          {
            description: "Declarative distribution reporting capability",
            id: "distribution-reporting",
            version: "1.0.0",
            workflowIds: ["distribution-reporting"],
          },
          {
            description: "Neutral declarative parameter adjustment capability",
            id: "parameter-adjustment",
            version: "1.0.0",
            workflowIds: ["parameter-adjustment"],
          },
        ],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [
          {
            capabilityId: "distribution-reporting",
            description: "Distribution reporting",
            id: "distribution-reporting",
            inputContract: "distribution_reporting_input",
            outputContract: "distribution_reporting_output",
            runnable: true,
            version: "1.0.0",
          },
          {
            capabilityId: "parameter-adjustment",
            description: "Parameter adjustment",
            id: "parameter-adjustment",
            inputContract: "parameter-input",
            outputContract: "parameter-value",
            runnable: true,
            version: "1.0.0",
          },
        ],
      }),
      outputs: vi.fn().mockResolvedValue({
        outputs: [{ id: "result", kind: "value", value: 10 }],
        runId: "parameter-adjustment-command-2",
      }),
      provideInput: vi.fn(),
      start: vi.fn().mockResolvedValue({
        capabilityId: "parameter-adjustment",
        commandId: "command-2",
        runId: "parameter-adjustment-command-2",
        status: "accepted",
        taskId: null,
        workflowId: "parameter-adjustment",
      }),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);

    expect(await screen.findByText(
      "Neutral declarative parameter adjustment capability",
    )).toBeVisible();
    await user.selectOptions(screen.getByRole("combobox", { name: "工作流" }), "parameter-adjustment");
    const input = screen.getByRole("textbox", { name: "运行输入 JSON" });
    await user.clear(input);
    await user.click(input);
    await user.paste('{"value":4}');
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith(
      { input: { value: 4 }, workflowId: "parameter-adjustment" },
      expect.any(String),
    ));
    expect(await screen.findByText("10")).toBeVisible();
    expect(screen.getByText("0 tokens")).toBeVisible();
    expect(screen.queryByRole("button", { name: "提交运行输入" })).not.toBeInTheDocument();
    expect(screen.queryByText(/sha/i)).not.toBeInTheDocument();
  });
});
