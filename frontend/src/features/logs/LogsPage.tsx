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

export function LogsPage({ api, platform, projectId }: LogsPageProps) {
  const [level, setLevel] = useState<EventLogLevel | "all">("all");
  const [search, setSearch] = useState("");
  const logs = useQuery({ queryFn: () => api.list(projectId), queryKey: ["event-logs", projectId] });
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
        <div><p>PROJECT EVENTS</p><h1>日志</h1></div>
        <button disabled={!logs.data} onClick={() => void download()} type="button">下载日志</button>
      </header>
      <div className="logs-page__filters">
        <label><span>筛选日志</span><input onChange={(event) => setSearch(event.target.value)} placeholder="搜索类型、消息或 Agent" type="search" value={search} /></label>
        <label><span>日志级别</span><select onChange={(event) => setLevel(event.target.value as EventLogLevel | "all")} value={level}>
          <option value="all">全部</option><option value="info">信息</option><option value="warning">警告</option><option value="error">错误</option>
        </select></label>
      </div>
      {logs.isPending ? <p role="status">正在加载日志…</p> : null}
      {logs.isError ? <div><p role="alert">日志加载失败</p><button onClick={() => void logs.refetch()} type="button">重新加载</button></div> : null}
      {logs.data && entries.length === 0 ? <p className="logs-page__empty">没有符合条件的日志</p> : null}
      {entries.length > 0 ? <ol className="logs-page__list">
        {entries.map((entry) => <li key={entry.eventId}>
          <time dateTime={entry.timestamp}>{displayTimestamp(entry.timestamp)}</time>
          <span className={`logs-page__level logs-page__level--${entry.level}`}>{entry.level}</span>
          <code>{entry.type}</code>
          <p>{entry.message}</p>
          {entry.agentId ? <small>{entry.agentId}</small> : null}
        </li>)}
      </ol> : null}
    </section>
  );
}

export function LogsRoutePage({ gateway, platform }: { readonly gateway: ApiGateway; readonly platform?: PlatformBridge }) {
  const { projectId } = useParams();
  const api = useMemo(() => createLogApi(gateway), [gateway]);
  if (!projectId) return <p role="alert">日志路由无效</p>;
  return <LogsPage api={api} projectId={projectId} {...(platform ? { platform } : {})} />;
}
