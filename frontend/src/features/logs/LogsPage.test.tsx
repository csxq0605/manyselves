import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/providers";
import type { PlatformBridge } from "../../platform/types";
import type { EventLogResponse, LogApi } from "./log-api";
import { LogsPage } from "./LogsPage";

const response: EventLogResponse = {
  entries: [
    {
      agentId: "main",
      eventId: "stream-1:evt-1",
      level: "info",
      message: "任务开始",
      sessionId: "session-1",
      timestamp: "2026-08-04T08:00:00Z",
      type: "task.status.changed",
    },
    {
      agentId: "researcher",
      eventId: "stream-1:evt-2",
      level: "error",
      message: "工具执行失败",
      sessionId: "session-1",
      timestamp: "2026-08-04T08:01:00Z",
      type: "tool.failed",
    },
  ],
  projectId: "project-1",
  total: 2,
};

function renderPage(api: LogApi, platform?: PlatformBridge) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <AppProviders queryClient={queryClient}>
      <LogsPage api={api} projectId="project-1" {...(platform ? { platform } : {})} />
    </AppProviders>,
  );
}

function fakeApi(overrides: Partial<LogApi> = {}): LogApi {
  return {
    list: vi.fn().mockResolvedValue(response),
    search: vi.fn().mockResolvedValue([]),
    stats: vi.fn().mockResolvedValue({ total: 0, byLevel: {}, byType: {}, dateRange: { start: null, end: null } }),
    ...overrides,
  };
}

describe("LogsPage", () => {
  it("filters the sanitized read-only event projection", async () => {
    const user = userEvent.setup();
    renderPage(fakeApi());

    expect(await screen.findByRole("heading", { name: "日志" })).toBeVisible();
    expect(await screen.findByText("任务开始")).toBeVisible();
    expect(screen.getByText("工具执行失败")).toBeVisible();

    await user.selectOptions(screen.getByLabelText("日志级别"), "error");
    expect(screen.queryByText("任务开始")).not.toBeInTheDocument();
    expect(screen.getByText("工具执行失败")).toBeVisible();
    expect(screen.queryByRole("button", { name: /上传|新建目录|重命名|编辑|删除/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Work\/|\.manyselves/)).not.toBeInTheDocument();
  });

  it("loads a compact page and renders logs inside a scrollable viewport", async () => {
    const list = vi.fn().mockResolvedValue({
      ...response,
      total: 125,
    });
    renderPage(fakeApi({ list }));

    const viewport = await screen.findByRole("region", { name: "日志记录滚动区" });
    expect(viewport).toHaveClass("logs-page__viewport");
    await waitFor(() => expect(list).toHaveBeenCalledWith("project-1", 50, 0));
    expect(screen.getByText("共 125 条日志")).toBeVisible();
    expect(screen.getByText("第 1 / 3 页")).toBeVisible();
  });

  it("downloads exactly the sanitized projection currently returned by the API", async () => {
    const user = userEvent.setup();
    const saveDownload = vi.fn().mockResolvedValue(undefined);
    const platform = {
      kind: "browser",
      notify: vi.fn(),
      openDownloadedFile: vi.fn(),
      saveDownload,
      selectDirectory: vi.fn(),
      selectFiles: vi.fn(),
    } satisfies PlatformBridge;
    renderPage(fakeApi(), platform);

    await user.click(await screen.findByRole("button", { name: "下载日志" }));
    expect(saveDownload).toHaveBeenCalledWith({
      blob: expect.any(Blob),
      suggestedName: "project-1-events.json",
    });
  });
});
