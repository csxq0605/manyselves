import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import type { ApiGateway } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import { createLogApi, type EventLogLevel, type LogApi } from "./log-api";
import "./logs-page.css";
import "../runtime/runtime-page.css";

export interface LogsPageProps {
  readonly api: LogApi;
  readonly platform?: PlatformBridge;
  readonly projectId: string;
}

const PAGE_SIZE = 50;

function downloadInBrowser(blob: Blob, suggestedName: string) {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = suggestedName;
  anchor.click();
  URL.revokeObjectURL(href);
}

function displayTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function pageNumbers(page: number, totalPages: number): number[] {
  const visible = Math.min(5, totalPages);
  const start = Math.max(0, Math.min(page - 2, totalPages - visible));
  return Array.from({ length: visible }, (_, index) => start + index);
}

export function LogsPage({ api, platform, projectId }: LogsPageProps) {
  const [level, setLevel] = useState<EventLogLevel | "all">("all");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);

  const offset = page * PAGE_SIZE;
  const logs = useQuery({
    queryFn: () => api.list(projectId, PAGE_SIZE, offset),
    queryKey: ["event-logs", projectId, PAGE_SIZE, offset],
  });

  const total = logs.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const entries = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    return (logs.data?.entries ?? []).filter((entry) => (
      (level === "all" || entry.level === level)
      && (!term || `${entry.type} ${entry.message} ${entry.agentId ?? ""}`.toLocaleLowerCase().includes(term))
    ));
  }, [level, logs.data?.entries, search]);

  async function download() {
    if (!logs.data) return;
    const blob = new Blob([JSON.stringify(logs.data, null, 2)], { type: "application/json" });
    const suggestedName = `${projectId}-events.json`;
    if (platform) await platform.saveDownload({ blob, suggestedName });
    else downloadInBrowser(blob, suggestedName);
  }

  return (
    <section className="logs-page">
      <header className="operations-header">
        <div>
          <p>PROJECT EVENTS</p>
          <h1>日志</h1>
        </div>
        <button disabled={!logs.data} onClick={() => void download()} type="button">下载日志</button>
      </header>

      <div className="logs-page__filters">
        <label>
          <span>筛选日志</span>
          <input
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索类型、消息或 Agent"
            type="search"
            value={search}
          />
        </label>
        <label>
          <span>日志级别</span>
          <select
            onChange={(event) => setLevel(event.target.value as EventLogLevel | "all")}
            value={level}
          >
            <option value="all">全部</option>
            <option value="info">信息</option>
            <option value="warning">警告</option>
            <option value="error">错误</option>
          </select>
        </label>
      </div>

      <div className="logs-page__pagination-info">
        <span>共 {total} 条日志</span>
        <span>第 {page + 1} / {totalPages} 页</span>
      </div>

      {logs.isPending ? <p role="status">正在加载日志…</p> : null}
      {logs.isError ? (
        <div>
          <p role="alert">日志加载失败</p>
          <button onClick={() => void logs.refetch()} type="button">重新加载</button>
        </div>
      ) : null}

      {logs.data ? (
        <div aria-label="日志记录滚动区" className="logs-page__viewport" role="region">
          {entries.length === 0 ? (
            <p className="logs-page__empty">没有符合条件的日志</p>
          ) : (
            <ol className="logs-page__list">
              {entries.map((entry) => (
                <li key={entry.eventId}>
                  <time dateTime={entry.timestamp}>{displayTimestamp(entry.timestamp)}</time>
                  <span className={`logs-page__level logs-page__level--${entry.level}`}>{entry.level}</span>
                  <code>{entry.type}</code>
                  <p>{entry.message}</p>
                  {entry.agentId ? <small>{entry.agentId}</small> : null}
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}

      {totalPages > 1 ? (
        <div className="logs-page__pagination">
          <button
            disabled={page === 0}
            onClick={() => setPage((current) => Math.max(0, current - 1))}
            type="button"
          >
            上一页
          </button>
          <span className="logs-page__page-numbers">
            {pageNumbers(page, totalPages).map((pageNumber) => (
              <button
                key={pageNumber}
                className={pageNumber === page ? "active" : ""}
                onClick={() => setPage(pageNumber)}
                type="button"
              >
                {pageNumber + 1}
              </button>
            ))}
          </span>
          <button
            disabled={page >= totalPages - 1}
            onClick={() => setPage((current) => Math.min(totalPages - 1, current + 1))}
            type="button"
          >
            下一页
          </button>
        </div>
      ) : null}
    </section>
  );
}

export function LogsRoutePage({ gateway, platform }: { readonly gateway: ApiGateway; readonly platform?: PlatformBridge }) {
  const { projectId } = useParams();
  const api = useMemo(() => createLogApi(gateway), [gateway]);
  if (!projectId) return <p role="alert">日志路由无效</p>;
  return <LogsPage api={api} projectId={projectId} {...(platform ? { platform } : {})} />;
}
