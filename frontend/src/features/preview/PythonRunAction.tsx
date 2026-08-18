import { useEffect, useState } from "react";

import type { OperationApi, PythonOperation } from "./operation-api";

const terminalStatuses = new Set(["completed", "failed", "timed_out", "interrupted", "cancelled"]);

export interface PythonRunActionProps {
  readonly api: OperationApi;
  readonly path: string;
  readonly pollIntervalMs?: number;
}

export function PythonRunAction({ api, path, pollIntervalMs = 750 }: PythonRunActionProps) {
  const [operation, setOperation] = useState<PythonOperation | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!operationId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const latest = await api.get(operationId as string);
        if (cancelled) return;
        setOperation(latest);
        if (!terminalStatuses.has(latest.status) && pollIntervalMs > 0) {
          timer = setTimeout(() => void poll(), pollIntervalMs);
        }
      } catch {
        if (!cancelled) setError("运行状态读取失败");
      }
    }
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [api, operationId, pollIntervalMs]);

  async function run() {
    if (!window.confirm(
      `将在可信服务器环境中运行 ${path}。该操作不提供操作系统级沙箱，请仅运行你信任的代码。是否继续？`,
    )) return;
    setPending(true);
    setError(null);
    setOperation(null);
    try {
      const accepted = await api.run(path);
      setOperationId(accepted.operationId);
    } catch {
      setError("Python 运行请求失败");
    } finally {
      setPending(false);
    }
  }

  async function interrupt() {
    if (!operationId) return;
    setPending(true);
    setError(null);
    try {
      const interrupted = await api.interrupt(operationId);
      setOperationId(null);
      setOperation(interrupted);
    } catch {
      setError("中断请求失败");
    } finally {
      setPending(false);
    }
  }

  const running = Boolean(operationId) && (!operation || !terminalStatuses.has(operation.status));
  return (
    <section aria-label={`Python 运行 ${path}`} className="python-run-action">
      <div className="preview-toolbar">
        <button disabled={pending || running} onClick={() => void run()} type="button">在可信服务器运行</button>
        {running ? <button disabled={pending} onClick={() => void interrupt()} type="button">中断运行</button> : null}
        {operation ? <span role="status">状态：{operation.status}</span> : null}
      </div>
      <p className="trusted-runtime-note">仅适用于 Linux Compose 可信服务器；不提供操作系统级代码沙箱。</p>
      {error ? <p role="alert">{error}</p> : null}
      {operation?.stdout ? <pre aria-label="标准输出">{operation.stdout}</pre> : null}
      {operation?.stdoutTruncated ? <p role="status">标准输出已截断</p> : null}
      {operation?.stderr ? <pre aria-label="标准错误">{operation.stderr}</pre> : null}
      {operation?.stderrTruncated ? <p role="status">标准错误已截断</p> : null}
    </section>
  );
}
