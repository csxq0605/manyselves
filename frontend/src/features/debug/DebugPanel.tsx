import { maskSensitive, type RuntimeDebugView, type RuntimeUsageView } from "../agents/event-reducer";

export interface DebugPanelProps {
  readonly entries: readonly RuntimeDebugView[];
  readonly usage: RuntimeUsageView;
}

function errorText(error: unknown): string {
  const masked = maskSensitive(error);
  return typeof masked === "string" ? masked : JSON.stringify(masked, null, 2);
}

export function DebugPanel({ entries, usage }: DebugPanelProps) {
  if (entries.length === 0) {
    return null;
  }
  return (
    <section className="runtime-section" aria-labelledby="runtime-debug-heading">
      <div className="runtime-section__heading">
        <h3 id="runtime-debug-heading">API 调试</h3>
        <span>{usage.tokensIn + usage.tokensOut} tokens · {usage.durationMs} ms</span>
      </div>
      <ul className="runtime-cards">
        {entries.map((entry) => (
          <li key={entry.id}>
            <header>
              <strong>{entry.agentId} · {entry.model}</strong>
              <span className={`runtime-badge runtime-badge--${entry.status}`}>
                {entry.status === "error" ? "API 调用失败" : entry.status}
              </span>
            </header>
            <small>{entry.tokensIn} in / {entry.tokensOut} out · {entry.durationMs} ms</small>
            {entry.error ? (
              <details>
                <summary>错误详情</summary>
                <pre>{errorText(entry.error)}</pre>
              </details>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
