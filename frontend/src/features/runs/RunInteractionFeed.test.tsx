import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { RunInteractionFeed } from "./RunInteractionFeed";
import type { WorkflowApi, WorkflowRunFeedApi } from "./workflow-api";

it("keeps a persisted completed run and its cost visible after reopening the conversation", async () => {
  const api = {
    listRuns: vi.fn(async () => ({ runs: [{
      run: { runId: "finished-1", workflowId: "full-report", status: "completed", active: false },
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
  expect(await screen.findByText(/Token Plan Lite/)).toBeVisible();
  await userEvent.click(await screen.findByText("本次运行记录的输出路径"));
  expect(await screen.findByText("Work/runs/finished-1/delivery/report.docx")).toBeVisible();
  expect(screen.queryByRole("button", { name: "恢复原报告" })).not.toBeInTheDocument();
  first.unmount();
  render(view());
  expect(await screen.findByText("配电安全报告已完成")).toBeVisible();
  expect(api.listRuns).toHaveBeenCalledWith("owner-1");
});
