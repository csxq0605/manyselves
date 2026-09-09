import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { RunInteractionFeed } from "./RunInteractionFeed";
import type { WorkflowApi, WorkflowRunFeedApi } from "./workflow-api";

it("keeps only a compact latest status in chat and loads cost on demand", async () => {
  const api = {
    listRuns: vi.fn(async () => ({ runs: [{
      run: { runId: "finished-1", workflowId: "full-report", status: "completed", active: false },
      state: {}, waitingInput: [],
    }, {
      run: { runId: "old-finished", workflowId: "module-report", status: "completed", active: false },
      state: {}, waitingInput: [],
    }, {
      run: { runId: "old-failed", workflowId: "aggregate-existing", status: "failed", active: false },
      state: {}, waitingInput: [],
    }] })),
    cost: vi.fn(async () => ({ runId: "finished-1", usage: {
      totals: { total_tokens: 123456, provider_attempts: 8 },
      pricing_summary: "MiMo 成本换算：Token Plan Lite 月付折算 ¥1.20",
    } })),
    outputs: vi.fn(async () => ({ runId: "finished-1", outputs: [
      { id: "report", path: "Work/runs/finished-1/delivery/report.docx", exists: true },
    ] })),
  } as unknown as WorkflowApi & WorkflowRunFeedApi;
  const view = () => <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <RunInteractionFeed api={api} conversationId="owner-1" projectId="test" />
  </QueryClientProvider>;
  const first = render(view());
  expect(await screen.findByText("配电安全报告已完成")).toBeVisible();
  expect(screen.queryByText(/Token Plan Lite/)).not.toBeInTheDocument();
  expect(screen.queryByText("old-finished")).not.toBeInTheDocument();
  expect(screen.queryByText(/old-failed/)).not.toBeInTheDocument();
  expect(screen.queryByText(/更早完成的运行/)).not.toBeInTheDocument();
  expect(api.cost).not.toHaveBeenCalled();
  expect(api.outputs).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "查看本次运行详情与成本" }));
  expect(screen.getByRole("dialog", { name: "运行详情与成本" })).toBeVisible();
  expect(await screen.findByText(/Token Plan Lite/)).toBeVisible();
  await userEvent.click(await screen.findByText("本次运行记录的输出路径"));
  expect(await screen.findByText("Work/runs/finished-1/delivery/report.docx")).toBeVisible();
  expect(screen.queryByRole("button", { name: "恢复原报告" })).not.toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看本次运行详情与成本" })).toHaveFocus();
  first.unmount();
  render(view());
  expect(await screen.findByText("配电安全报告已完成")).toBeVisible();
  expect(api.listRuns).toHaveBeenCalledWith("owner-1");
});

it("keeps older failures behind the attention button and reports a failed resume", async () => {
  const api = {
    listRuns: vi.fn(async () => ({ runs: [{
      run: { runId: "latest", workflowId: "full-report", status: "completed", active: false },
      state: {}, waitingInput: [],
    }, {
      run: { runId: "interrupted", workflowId: "aggregate-existing", status: "failed", active: false },
      state: { error: "原始失败原因" }, waitingInput: [],
    }] })),
    resume: vi.fn(async () => { throw new Error("服务暂时不可用"); }),
  } as unknown as WorkflowApi & WorkflowRunFeedApi;
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <RunInteractionFeed api={api} conversationId="owner-1" projectId="test" />
  </QueryClientProvider>);
  expect(await screen.findByText("配电安全报告已完成")).toBeVisible();
  expect(screen.queryByText("原始失败原因")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "处理运行（1）" }));
  expect(screen.getByText("原始失败原因")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "恢复原报告" }));
  await waitFor(() => expect(screen.getByText("恢复失败：服务暂时不可用")).toBeVisible());
  expect(api.resume).toHaveBeenCalledWith("interrupted", expect.any(String));
});
