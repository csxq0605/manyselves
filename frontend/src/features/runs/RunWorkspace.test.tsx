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
        usage: { totals: { estimated_cost: 1.25, pricing_status: "configured", total_tokens: 15 } },
      }),
      events: vi.fn().mockResolvedValue({
        events: Array.from({ length: 105 }, (_, index) => ({
          actionId: null,
          data: {},
          error: null,
          kind: index === 104 ? "workflow.completed" : `event.${index}`,
          runId: "run-1",
          workflowId: "distribution-reporting",
        })),
        runId: "run-1",
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
        schema: {
          properties: { instruction: { type: "string" } },
          required: ["instruction"],
          type: "object",
        },
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
      resume: vi.fn(),
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
    const input = await screen.findByRole("textbox", { name: "instruction" });
    await user.clear(input);
    await user.click(input);
    await user.paste("Generate report");
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith(
      { input: { instruction: "Generate report" }, workflowId: "distribution-reporting" },
      expect.any(String),
    ));
    expect(await screen.findByText("completed")).toBeVisible();
    expect(screen.getByText("Outputs/Reports/report.docx")).toBeVisible();
    expect(screen.getByText("15 tokens")).toBeVisible();
    expect(screen.getByText("定价状态：configured")).toBeVisible();
    expect(screen.getByText("1.25")).toBeVisible();
    expect(await screen.findByText("workflow.completed")).toBeVisible();
    expect(screen.getByText("最近 100 条")).toBeVisible();
    expect(screen.queryByText("event.0")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "提交运行输入" })).not.toBeInTheDocument();
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
        schema: workflowId === "parameter-adjustment"
          ? {
            properties: { value: { type: "integer" } },
            required: ["value"],
            type: "object",
          }
          : { type: "object" },
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
      resume: vi.fn(),
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
    const input = await screen.findByRole("spinbutton", { name: "value" });
    expect(screen.queryByRole("textbox", { name: "instruction" })).not.toBeInTheDocument();
    await user.type(input, "4");
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

  it("starts a workflow with a primitive root input contract", async () => {
    const start = vi.fn().mockResolvedValue({
      capabilityId: "primitive-capability",
      commandId: "primitive-command",
      runId: "primitive-run",
      status: "accepted",
      workflowId: "primitive-workflow",
    });
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({ runId: "primitive-run", usage: { totals: {} } }),
      get: vi.fn().mockResolvedValue({
        run: {
          active: false,
          capabilityId: "primitive-capability",
          runId: "primitive-run",
          status: "completed",
          workflowId: "primitive-workflow",
        },
        state: {},
        waitingInput: [],
      }),
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "primitive-input",
        schema: { type: "integer" },
        workflowId: "primitive-workflow",
      }),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [{
          description: "Primitive capability",
          id: "primitive-capability",
          version: "1.0.0",
          workflowIds: ["primitive-workflow"],
        }],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [{
          capabilityId: "primitive-capability",
          description: "Primitive workflow",
          id: "primitive-workflow",
          inputContract: "primitive-input",
          outputContract: "primitive-output",
          runnable: true,
          version: "1.0.0",
        }],
      }),
      outputs: vi.fn().mockResolvedValue({ outputs: [], runId: "primitive-run" }),
      provideInput: vi.fn(),
      resume: vi.fn(),
      start,
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);
    const input = await screen.findByRole("textbox", { name: "运行输入 JSON" });
    await user.clear(input);
    await user.type(input, "4");
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    await waitFor(() => expect(start).toHaveBeenCalledWith(
      { input: 4, workflowId: "primitive-workflow" },
      expect.any(String),
    ));
  });

  it("renders common schema controls and submits typed values", async () => {
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "run-schema-controls",
        usage: { totals: { estimated_cost: 0, total_tokens: 0 } },
      }),
      get: vi.fn().mockResolvedValue({
        run: {
          active: false,
          capabilityId: "neutral-controls",
          runId: "run-schema-controls",
          status: "completed",
          taskId: null,
          workflowId: "neutral-controls",
        },
        state: {},
        waitingInput: [],
      }),
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "neutral-controls-input",
        schema: {
          properties: {
            enabled: { type: "boolean" },
            name: { title: "Display name", type: "string" },
            ratio: { type: "number" },
            count: { type: "integer" },
            mode: { enum: ["fast", "safe"], type: "string" },
          },
          required: ["name", "count"],
          type: "object",
        },
        workflowId: "neutral-controls",
      }),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [{
          description: "Neutral controls",
          id: "neutral-controls",
          version: "1.0.0",
          workflowIds: ["neutral-controls"],
        }],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [{
          capabilityId: "neutral-controls",
          description: "Neutral controls",
          id: "neutral-controls",
          inputContract: "neutral-controls-input",
          outputContract: "neutral-controls-output",
          runnable: true,
          version: "1.0.0",
        }],
      }),
      outputs: vi.fn().mockResolvedValue({ outputs: [], runId: "run-schema-controls" }),
      provideInput: vi.fn(),
      resume: vi.fn(),
      start: vi.fn().mockResolvedValue({
        capabilityId: "neutral-controls",
        commandId: "command-schema-controls",
        runId: "run-schema-controls",
        status: "accepted",
        taskId: null,
        workflowId: "neutral-controls",
      }),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);

    expect(await screen.findByText("Neutral controls")).toBeVisible();
    await user.type(await screen.findByRole("textbox", { name: "Display name" }), "Ada");
    await user.type(await screen.findByRole("spinbutton", { name: "ratio" }), "1.5");
    await user.type(await screen.findByRole("spinbutton", { name: "count" }), "3");
    await user.click(await screen.findByRole("checkbox", { name: "enabled" }));
    await user.selectOptions(await screen.findByRole("combobox", { name: "mode" }), "safe");
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith(
      {
        input: { count: 3, enabled: true, mode: "safe", name: "Ada", ratio: 1.5 },
        workflowId: "neutral-controls",
      },
      expect.any(String),
    ));
  });

  it("renders mixed report schemas with nullable, array, and JSON fields", async () => {
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "run-report-schema",
        usage: { totals: { estimated_cost: 0, total_tokens: 0 } },
      }),
      get: vi.fn().mockResolvedValue({
        run: {
          active: false,
          capabilityId: "neutral-report-form",
          runId: "run-report-schema",
          status: "completed",
          taskId: null,
          workflowId: "report-request-form",
        },
        state: {},
        waitingInput: [],
      }),
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "report-request-input",
        schema: {
          $defs: {
            UserSupplement: {
              properties: { content: { type: "string" } },
              type: "object",
            },
          },
          properties: {
            instruction: {
              description: "Report instruction",
              minLength: 1,
              type: "string",
            },
            operation: {
              default: "full_report",
              enum: ["full_report", "module_report"],
              type: "string",
            },
            missing_evidence_policy: {
              default: "draft",
              enum: ["ask", "block", "skip", "draft"],
              type: "string",
            },
            cost_control_mode: {
              default: "observe",
              enum: ["observe", "warn", "pause_at_boundary"],
              type: "string",
            },
            nullable_note: {
              anyOf: [{ type: "string" }, { type: "null" }],
              default: null,
              title: "Nullable note",
            },
            nullable_ratio: {
              anyOf: [{ type: "number" }, { type: "null" }],
              default: null,
              title: "Nullable ratio",
            },
            nullable_enabled: {
              anyOf: [{ type: "boolean" }, { type: "null" }],
              default: null,
              title: "Nullable enabled",
            },
            target_modules: {
              default: ["2.1"],
              items: { type: "string" },
              title: "Target modules",
              type: "array",
            },
            max_provider_attempts: {
              default: 80,
              maximum: 1000,
              minimum: 1,
              type: "integer",
            },
            max_total_tokens: {
              default: 800000,
              minimum: 1000,
              type: "integer",
            },
            enabled: {
              default: false,
              description: "Required boolean remains optional to check",
              type: "boolean",
            },
            config: {
              anyOf: [
                {
                  properties: { mode: { type: "string" } },
                  type: "object",
                },
                { type: "null" },
              ],
              description: "Advanced configuration JSON",
              title: "Config",
            },
            supplement: {
              "$ref": "#/$defs/UserSupplement",
              title: "Supplement",
            },
            ambiguous_value: {
              anyOf: [{ type: "string" }, { type: "integer" }],
              title: "Ambiguous value",
            },
          },
          required: ["instruction", "enabled"],
          type: "object",
        },
        workflowId: "report-request-form",
      }),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [{
          description: "Neutral report form",
          id: "neutral-report-form",
          version: "1.0.0",
          workflowIds: ["report-request-form"],
        }],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [{
          capabilityId: "neutral-report-form",
          description: "Report request form",
          id: "report-request-form",
          inputContract: "report-request-input",
          outputContract: "report-request-output",
          runnable: true,
          version: "1.0.0",
        }],
      }),
      outputs: vi.fn().mockResolvedValue({ outputs: [], runId: "run-report-schema" }),
      provideInput: vi.fn(),
      resume: vi.fn(),
      start: vi.fn().mockResolvedValue({
        capabilityId: "neutral-report-form",
        commandId: "command-report-schema",
        runId: "run-report-schema",
        status: "accepted",
        taskId: null,
        workflowId: "report-request-form",
      }),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);

    await user.type(await screen.findByRole("textbox", { name: "instruction" }), "Draft report");
    expect(await screen.findByText("Report instruction")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "operation" })).toHaveValue("\"full_report\"");
    expect(screen.getByRole("combobox", { name: "missing_evidence_policy" })).toHaveValue("\"draft\"");
    expect(screen.getByRole("combobox", { name: "cost_control_mode" })).toHaveValue("\"observe\"");
    expect(screen.getByRole("spinbutton", { name: "max_provider_attempts" })).toHaveValue(80);
    expect(screen.getByRole("spinbutton", { name: "max_provider_attempts" })).toHaveAttribute("min", "1");
    expect(screen.getByRole("spinbutton", { name: "max_provider_attempts" })).toHaveAttribute("max", "1000");
    expect(screen.getByRole("spinbutton", { name: "max_total_tokens" })).toHaveValue(800000);
    expect(screen.getByRole("spinbutton", { name: "max_total_tokens" })).toHaveAttribute("min", "1000");
    expect(screen.getByRole("spinbutton", { name: "Nullable ratio" })).toHaveValue(null);
    expect(screen.getByRole("combobox", { name: "Nullable enabled" })).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: "enabled" })).not.toBeRequired();

    const nullable = screen.getByRole("textbox", { name: "Nullable note" });
    await user.type(nullable, "optional");
    await user.clear(nullable);
    await user.clear(screen.getByRole("textbox", { name: "Target modules" }));
    await user.paste("2.1\n2.2");
    await user.clear(screen.getByRole("spinbutton", { name: "max_provider_attempts" }));
    await user.type(screen.getByRole("spinbutton", { name: "max_provider_attempts" }), "120");
    await user.click(screen.getByRole("textbox", { name: "Config" }));
    await user.paste('{"mode":"observe"}');
    await user.click(screen.getByRole("textbox", { name: "Supplement" }));
    await user.paste('{"content":"fact"}');
    await user.click(screen.getByRole("textbox", { name: "Ambiguous value" }));
    await user.paste("5");
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    await waitFor(() => expect(api.start).toHaveBeenCalledWith(
      {
        input: {
          config: { mode: "observe" },
          cost_control_mode: "observe",
          enabled: false,
          instruction: "Draft report",
          max_provider_attempts: 120,
          max_total_tokens: 800000,
          missing_evidence_policy: "draft",
          nullable_note: null,
          nullable_enabled: null,
          nullable_ratio: null,
          operation: "full_report",
          supplement: { content: "fact" },
          target_modules: ["2.1", "2.2"],
          ambiguous_value: 5,
        },
        workflowId: "report-request-form",
      },
      expect.any(String),
    ));
  });

  it("isolates nested waiting values by path and displays the input context", async () => {
    const provideInput = vi.fn().mockResolvedValue({
      capabilityId: "distribution-reporting",
      commandId: "command-input",
      runId: "report-declarative-waiting",
      status: "accepted",
      taskId: "task-input",
      workflowId: "distribution-reporting",
    });
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "report-declarative-waiting",
        usage: { totals: { estimated_cost: 1, total_tokens: 20 } },
      }),
      get: vi.fn()
        .mockResolvedValueOnce({
          run: {
            active: false,
            capabilityId: "distribution-reporting",
            runId: "report-declarative-waiting",
            status: "waiting",
            taskId: "task-input",
            workflowId: "distribution-reporting",
          },
          state: {},
          waitingInput: [{
            contract_id: "supplement-contract",
            description: "请填写当前子流程所需资料。",
            input_id: "ask-child",
            path: [
              { action_id: "run-parent", kind: "subworkflow" },
              { action_id: "run-child-a", kind: "subworkflow" },
            ],
            schema: { type: "object" },
            title: "子流程补充资料",
          }],
        })
        .mockResolvedValue({
          run: {
            active: false,
            capabilityId: "distribution-reporting",
            runId: "report-declarative-waiting",
            status: "waiting",
            taskId: "task-input",
            workflowId: "distribution-reporting",
          },
          state: {},
          waitingInput: [{
            contract_id: "supplement-contract",
            description: "请填写当前子流程所需资料。",
            input_id: "ask-child",
            path: [
              { action_id: "run-parent", kind: "subworkflow" },
              { action_id: "run-child-b", kind: "subworkflow" },
            ],
            schema: { type: "object" },
            title: "子流程补充资料",
          }],
        }),
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "distribution_reporting_input",
        schema: { type: "object" },
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
        outputs: [],
        runId: "report-declarative-waiting",
      }),
      provideInput,
      resume: vi.fn(),
      start: vi.fn().mockResolvedValue({
        capabilityId: "distribution-reporting",
        commandId: "command-start",
        runId: "report-declarative-waiting",
        status: "accepted",
        taskId: "task-input",
        workflowId: "distribution-reporting",
      }),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);
    await screen.findByText("Declarative distribution reporting capability");
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    expect(await screen.findByRole("heading", { name: "子流程补充资料" })).toBeVisible();
    expect(screen.getByText("请填写当前子流程所需资料。")).toBeVisible();
    expect(screen.getByText("路径：run-parent → run-child-a")).toBeVisible();
    const continuation = await screen.findByRole("textbox", { name: "继续输入 JSON" });
    await user.clear(continuation);
    await user.click(continuation);
    await user.paste('{"answer":"Ada"}');
    await user.click(screen.getByRole("button", { name: "提交运行输入" }));

    await waitFor(() => expect(provideInput).toHaveBeenCalledWith(
      "report-declarative-waiting",
      { inputId: "ask-child", values: { answer: "Ada" } },
      expect.any(String),
    ));
    expect(await screen.findByText("路径：run-parent → run-child-b")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "继续输入 JSON" })).toHaveValue("{}");
  });

  it("submits a primitive value for a primitive waiting contract", async () => {
    const provideInput = vi.fn().mockResolvedValue({
      capabilityId: "parameter-adjustment",
      commandId: "command-input",
      runId: "primitive-waiting",
      status: "accepted",
      workflowId: "parameter-adjustment",
    });
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "primitive-waiting",
        usage: { totals: { pricing_status: "unconfigured", total_tokens: 0 } },
      }),
      get: vi.fn().mockResolvedValue({
        run: {
          active: false,
          capabilityId: "parameter-adjustment",
          runId: "primitive-waiting",
          status: "waiting",
          workflowId: "parameter-adjustment",
        },
        state: {},
        waitingInput: [{ input_id: "ask-number", schema: { type: "integer" } }],
      }),
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "parameter-input",
        schema: { type: "object" },
        workflowId: "parameter-adjustment",
      }),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [{
          description: "Neutral parameter adjustment",
          id: "parameter-adjustment",
          version: "1.0.0",
          workflowIds: ["parameter-adjustment"],
        }],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [{
          capabilityId: "parameter-adjustment",
          description: "Neutral parameter adjustment",
          id: "parameter-adjustment",
          inputContract: "parameter-input",
          outputContract: "parameter-value",
          runnable: true,
          version: "1.0.0",
        }],
      }),
      outputs: vi.fn().mockResolvedValue({ outputs: [], runId: "primitive-waiting" }),
      provideInput,
      resume: vi.fn(),
      start: vi.fn().mockResolvedValue({
        capabilityId: "parameter-adjustment",
        commandId: "command-start",
        runId: "primitive-waiting",
        status: "accepted",
        workflowId: "parameter-adjustment",
      }),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);
    await screen.findByText("Neutral parameter adjustment");
    await user.click(screen.getByRole("button", { name: "启动工作流" }));
    const continuation = await screen.findByRole("textbox", { name: "继续输入 JSON" });
    await user.clear(continuation);
    await user.type(continuation, "7");
    await user.click(screen.getByRole("button", { name: "提交运行输入" }));

    await waitFor(() => expect(provideInput).toHaveBeenCalledWith(
      "primitive-waiting",
      { inputId: "ask-number", values: 7 },
      expect.any(String),
    ));
    expect(screen.getByText("0 tokens")).toBeVisible();
    expect(screen.getByText("定价状态：unconfigured")).toBeVisible();
    expect(screen.getByText("成本未知")).toBeVisible();
  });

  it("distinguishes cost loading and API failure from an unpriced result", async () => {
    let rejectCost!: (reason: Error) => void;
    const cost = vi.fn().mockImplementation(() => new Promise((_resolve, reject) => {
      rejectCost = reject;
    }));
    const api: WorkflowApi = {
      cost,
      get: vi.fn().mockResolvedValue({
        run: {
          active: false,
          capabilityId: "parameter-adjustment",
          runId: "cost-failure",
          status: "completed",
          workflowId: "parameter-adjustment",
        },
        state: {},
        waitingInput: [],
      }),
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "parameter-input",
        schema: { type: "object" },
        workflowId: "parameter-adjustment",
      }),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [{
          description: "Neutral parameter adjustment",
          id: "parameter-adjustment",
          version: "1.0.0",
          workflowIds: ["parameter-adjustment"],
        }],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [{
          capabilityId: "parameter-adjustment",
          description: "Neutral parameter adjustment",
          id: "parameter-adjustment",
          inputContract: "parameter-input",
          outputContract: "parameter-value",
          runnable: true,
          version: "1.0.0",
        }],
      }),
      outputs: vi.fn().mockResolvedValue({ outputs: [], runId: "cost-failure" }),
      provideInput: vi.fn(),
      resume: vi.fn(),
      start: vi.fn().mockResolvedValue({
        capabilityId: "parameter-adjustment",
        commandId: "cost-failure-command",
        runId: "cost-failure",
        status: "accepted",
        workflowId: "parameter-adjustment",
      }),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);
    await screen.findByText("Neutral parameter adjustment");
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    expect(await screen.findByText("正在加载成本…")).toHaveAttribute("role", "status");
    expect(screen.queryByText("成本未知")).not.toBeInTheDocument();
    rejectCost(new Error("cost unavailable"));
    expect(await screen.findByText("成本加载失败。")).toHaveAttribute("role", "alert");
    expect(screen.queryByText("成本未知")).not.toBeInTheDocument();
  });

  it("polls an active run and refreshes its completed output", async () => {
    const get = vi.fn()
      .mockResolvedValueOnce({
        run: {
          active: true,
          capabilityId: "distribution-reporting",
          runId: "report-declarative-polling",
          status: "running",
          taskId: "task-polling",
          workflowId: "distribution-reporting",
        },
        state: {},
        waitingInput: [],
      })
      .mockResolvedValue({
        run: {
          active: false,
          capabilityId: "distribution-reporting",
          runId: "report-declarative-polling",
          status: "completed",
          taskId: "task-polling",
          workflowId: "distribution-reporting",
        },
        state: {},
        waitingInput: [],
      });
    const outputs = vi.fn()
      .mockResolvedValueOnce({ outputs: [], runId: "report-declarative-polling" })
      .mockResolvedValue({
        outputs: [{
          exists: true,
          id: "Outputs/Reports/polling.docx",
          kind: "artifact",
          path: "Outputs/Reports/polling.docx",
          size: 8,
        }],
        runId: "report-declarative-polling",
      });
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "report-declarative-polling",
        usage: { totals: { estimated_cost: 0, total_tokens: 0 } },
      }),
      get,
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "distribution_reporting_input",
        schema: { type: "object" },
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
      outputs,
      provideInput: vi.fn(),
      resume: vi.fn(),
      start: vi.fn().mockResolvedValue({
        capabilityId: "distribution-reporting",
        commandId: "command-polling",
        runId: "report-declarative-polling",
        status: "accepted",
        taskId: "task-polling",
        workflowId: "distribution-reporting",
      }),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);
    await screen.findByText("Declarative distribution reporting capability");
    await user.click(screen.getByRole("button", { name: "启动工作流" }));

    expect(await screen.findByText("running")).toBeVisible();
    expect(screen.queryByRole("button", { name: "恢复运行" })).not.toBeInTheDocument();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2), { timeout: 2500 });
    expect(await screen.findByText("completed")).toBeVisible();
    expect(await screen.findByText("Outputs/Reports/polling.docx")).toBeVisible();
    expect(outputs).toHaveBeenCalledTimes(2);
  });

  it.each(["running", "failed"] as const)(
    "resumes a persisted %s run through the same Run and polling lifecycle",
    async (persistedStatus) => {
    const get = vi.fn()
      .mockResolvedValueOnce({
        run: {
          active: false,
          capabilityId: "neutral-capability",
          runId: "persisted-running",
          status: persistedStatus,
          taskId: "task-persisted",
          workflowId: "neutral-workflow",
        },
        state: {},
        waitingInput: [],
      })
      .mockResolvedValueOnce({
        run: {
          active: true,
          capabilityId: "neutral-capability",
          runId: "persisted-running",
          status: "running",
          taskId: "task-persisted",
          workflowId: "neutral-workflow",
        },
        state: {},
        waitingInput: [],
      })
      .mockResolvedValue({
        run: {
          active: false,
          capabilityId: "neutral-capability",
          runId: "persisted-running",
          status: "completed",
          taskId: "task-persisted",
          workflowId: "neutral-workflow",
        },
        state: {},
        waitingInput: [],
      });
    const resume = vi.fn().mockResolvedValue({
      capabilityId: "neutral-capability",
      commandId: "command-resume",
      runId: "persisted-running",
      status: "accepted",
      taskId: "task-persisted",
      workflowId: "neutral-workflow",
    });
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "persisted-running",
        usage: { totals: { estimated_cost: 0, total_tokens: 0 } },
      }),
      events: vi.fn().mockResolvedValue({ events: [], runId: "persisted-running" }),
      get,
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "neutral-input",
        schema: { type: "object" },
        workflowId: "neutral-workflow",
      }),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [{
          description: "Neutral capability",
          id: "neutral-capability",
          version: "1.0.0",
          workflowIds: ["neutral-workflow"],
        }],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [{
          capabilityId: "neutral-capability",
          description: "Neutral workflow",
          id: "neutral-workflow",
          inputContract: "neutral-input",
          outputContract: "neutral-output",
          runnable: true,
          version: "1.0.0",
        }],
      }),
      outputs: vi.fn().mockResolvedValue({ outputs: [], runId: "persisted-running" }),
      provideInput: vi.fn(),
      resume,
      start: vi.fn(),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);
    await user.type(await screen.findByRole("textbox", { name: "Run ID" }), "persisted-running");
    await user.click(screen.getByRole("button", { name: "打开运行" }));

    await user.click(await screen.findByRole("button", { name: "恢复运行" }));
    await waitFor(() => expect(resume).toHaveBeenCalledWith("persisted-running", expect.any(String)));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(3), { timeout: 4000 });
    expect(await screen.findByText("completed")).toBeVisible();
    },
  );

  it("opens an existing run by Run ID", async () => {
    const get = vi.fn().mockResolvedValue({
      run: {
        active: false,
        capabilityId: "neutral-capability",
        runId: "existing-run",
        status: "completed",
        taskId: null,
        workflowId: "neutral-workflow",
      },
      state: {},
      waitingInput: [],
    });
    const api: WorkflowApi = {
      cost: vi.fn().mockResolvedValue({
        runId: "existing-run",
        usage: { totals: { total_tokens: 3 } },
      }),
      events: vi.fn().mockResolvedValue({ events: [], runId: "existing-run" }),
      get,
      inputSchema: vi.fn().mockResolvedValue({
        contractId: "neutral-input",
        schema: { type: "object" },
        workflowId: "neutral-workflow",
      }),
      listCapabilities: vi.fn().mockResolvedValue({
        capabilities: [{
          description: "Neutral capability",
          id: "neutral-capability",
          version: "1.0.0",
          workflowIds: ["neutral-workflow"],
        }],
      }),
      listWorkflows: vi.fn().mockResolvedValue({
        workflows: [{
          capabilityId: "neutral-capability",
          description: "Neutral workflow",
          id: "neutral-workflow",
          inputContract: "neutral-input",
          outputContract: "neutral-output",
          runnable: true,
          version: "1.0.0",
        }],
      }),
      outputs: vi.fn().mockResolvedValue({
        outputs: [{ id: "existing-output", kind: "value", value: "restored" }],
        runId: "existing-run",
      }),
      provideInput: vi.fn(),
      resume: vi.fn(),
      start: vi.fn(),
    };
    const user = userEvent.setup();

    render(<AppProviders><RunWorkspace api={api} /></AppProviders>);
    await user.type(await screen.findByRole("textbox", { name: "Run ID" }), "existing-run");
    await user.click(screen.getByRole("button", { name: "打开运行" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("existing-run"));
    expect(await screen.findByRole("heading", { name: "existing-run" })).toBeVisible();
    expect(await screen.findByText("restored")).toBeVisible();
  });
});
