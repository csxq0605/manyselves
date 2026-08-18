import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { PlatformBridge } from "../../platform/types";
import type { ReportingApi, ReportingSnapshotResponse } from "./reporting-api";
import { createReportingStore } from "./reporting-store";
import { ReportingWorkspace } from "./ReportingWorkspace";
import { RevisionView } from "./RevisionView";
import { RunProgress } from "./RunProgress";
import { normalizeReportingSnapshot } from "./reporting-store";
import { WaitingInputForm } from "./WaitingInputForm";

const snapshot: ReportingSnapshotResponse = {
  checkpoint: {
    activity: "delivery",
    completed_modules: ["2.1", "2.2"],
    preparation_refs: {
      coverage: "Work/runs/run-1/context/coverage.json",
      source_ledger: "Work/runs/run-1/context/source-ledger.json",
    },
    specialist_modules: ["2.1", "2.2", "2.3"],
    status: "completed",
  },
  evidence: { selected_action: "supplement" },
  outputs: [{
    exists: true,
    path: "Outputs/Reports/report.docx",
    sha256: "abc123",
    size: 1024,
  }],
  revision: { baseline_version_id: "version-1", feedback: "修正保护边界" },
  run: { active: false, run_id: "run-1", status: "completed" },
  state: {
    activity: "delivery",
    completed_modules: ["2.1", "2.2"],
    preparation_refs: {
      coverage: "Work/runs/run-1/context/coverage.json",
      source_ledger: "Work/runs/run-1/context/source-ledger.json",
    },
    specialist_modules: ["2.1", "2.2", "2.3"],
    status: "completed",
  },
  waitingInput: [],
};

function api(overrides: Partial<ReportingApi> = {}): ReportingApi {
  return {
    cancel: vi.fn(),
    decide: vi.fn(),
    download: vi.fn().mockResolvedValue(new Blob(["report"])),
    get: vi.fn().mockResolvedValue(snapshot),
    list: vi.fn().mockResolvedValue({ runs: [snapshot.run] }),
    resume: vi.fn(),
    revise: vi.fn(),
    start: vi.fn(),
    ...overrides,
  } as ReportingApi;
}

function platform(): PlatformBridge {
  return {
    kind: "browser",
    notify: vi.fn(),
    openDownloadedFile: vi.fn(),
    saveDownload: vi.fn(),
    selectDirectory: vi.fn(),
    selectFiles: vi.fn(),
  };
}

function wrapper({ children }: { readonly children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  })}>{children}</QueryClientProvider>;
}

describe("ReportingWorkspace", () => {
  it.each([
    ["planning", "正在规划"],
    ["running", "生成中"],
    ["waiting_user", "等待补充信息"],
    ["revising", "修订中"],
    ["completed", "已完成"],
    ["failed", "失败"],
  ] as const)("renders reporting state %s", (status, label) => {
    render(<RunProgress run={{
      active: status === "running",
      canCancel: status === "running",
      canResume: status === "failed",
      error: "",
      id: "run-1",
      operation: "full_report",
      rawStatus: status,
      status,
      taskId: "",
      title: "run-1",
    }} />);
    expect(screen.getByText(label)).toBeVisible();
  });

  it("submits waiting input once and disables duplicate submission", async () => {
    const decide = vi.fn().mockResolvedValue(undefined);
    render(<WaitingInputForm
      onSubmit={decide}
      request={{
        affectedModules: ["2.1"],
        allowedActions: ["supplement", "draft", "stop"],
        decisionId: "decision-1",
        missingItems: ["缺少保护定值"],
        selectedAction: "",
        status: "pending",
      }}
    />);
    await userEvent.selectOptions(screen.getByLabelText("处理方式"), "supplement");
    await userEvent.type(screen.getByLabelText("补充信息"), "保护定值单已上传");
    await userEvent.click(screen.getByRole("button", { name: "继续流程" }));
    await userEvent.click(screen.getByRole("button", { name: "继续流程" }));

    expect(decide).toHaveBeenCalledTimes(1);
    expect(decide).toHaveBeenCalledWith("decision-1", {
      action: "supplement",
      supplements: [{ content: "保护定值单已上传", scope: "run" }],
    });
    expect(screen.getByRole("button", { name: "继续流程" })).toBeDisabled();
  });

  it("loads the durable run before showing plan, evidence, revision and verified outputs", async () => {
    const reportingApi = api();
    const bridge = platform();
    render(<ReportingWorkspace
      api={reportingApi}
      platform={bridge}
      projectId="project-1"
      store={createReportingStore("stream-1")}
    />, { wrapper });

    expect(await screen.findByRole("heading", { name: "报告运行 run-1" })).toBeVisible();
    expect(screen.getByRole("list", { name: "报告模块" })).toHaveTextContent("2.1");
    expect(screen.getByText("source_ledger")).toBeVisible();
    expect(screen.getByText("version-1")).toBeVisible();
    expect(screen.getByText("服务端已验证交付文件")).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "下载 report.docx" }));
    await waitFor(() => expect(reportingApi.download).toHaveBeenCalledWith(
      "project-1",
      "Outputs/Reports/report.docx",
    ));
    expect(bridge.saveDownload).toHaveBeenCalledWith(expect.objectContaining({
      suggestedName: "report.docx",
    }));
  });

  it("keeps generated but invalid artifacts visibly failed and not downloadable", async () => {
    const invalid = {
      ...snapshot,
      outputs: [{ exists: false, path: "Outputs/Reports/broken.docx", sha256: null, size: 0 }],
    };
    render(<ReportingWorkspace
      api={api({ get: vi.fn().mockResolvedValue(invalid) })}
      platform={platform()}
      projectId="project-1"
      store={createReportingStore("stream-1")}
    />, { wrapper });

    expect(await screen.findByText("输出文件缺失或未通过服务端校验")).toBeVisible();
    expect(screen.queryByRole("button", { name: "下载 broken.docx" })).toBeNull();
  });

  it("does not offer revision before a verified baseline version is complete", () => {
    const running = normalizeReportingSnapshot({
      checkpoint: { activity: "dispatch", run_id: "run-1", status: "in_progress" },
      evidence: {}, outputs: [], revision: {},
      run: { active: true, run_id: "run-1", status: "running" },
      state: { activity: "dispatch", status: "in_progress" }, waitingInput: [],
    });
    render(<RevisionView onSubmit={vi.fn()} snapshot={running} />);

    expect(screen.queryByRole("button", { name: "创建修订运行" })).toBeNull();
    expect(screen.getByText("报告完成并通过服务端校验后可创建局部修订。")).toBeVisible();
  });

  it("reuses the same idempotency key when retrying one ambiguous start", async () => {
    const start = vi.fn()
      .mockRejectedValueOnce(new Error("connection lost"))
      .mockResolvedValueOnce({ commandId: "command-1", runId: "run-new", status: "accepted" });
    const reportingApi = api({
      get: vi.fn().mockResolvedValue({ ...snapshot, run: { ...snapshot.run, run_id: "run-new" } }),
      list: vi.fn().mockResolvedValue({ runs: [] }),
      start,
    });
    render(<ReportingWorkspace
      api={reportingApi}
      platform={platform()}
      projectId="project-1"
      store={createReportingStore("stream-1")}
    />, { wrapper });

    await userEvent.type(screen.getByLabelText("报告要求"), "生成报告");
    await userEvent.click(screen.getByRole("button", { name: "开始报告运行" }));
    expect(await screen.findByText("创建报告运行失败。")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "开始报告运行" }));

    await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
    expect(start.mock.calls[0]?.[1]).toBe(start.mock.calls[1]?.[1]);
  });
});
