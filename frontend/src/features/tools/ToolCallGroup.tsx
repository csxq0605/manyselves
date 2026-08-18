import { maskSensitive, type RuntimeToolView } from "../agents/event-reducer";

export interface ToolCallGroupProps {
  readonly tools: readonly RuntimeToolView[];
}

function formatted(value: unknown): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  try {
    return JSON.stringify(maskSensitive(value), null, 2);
  } catch {
    return "[无法显示的数据]";
  }
}

const statusLabels = {
  completed: "完成",
  failed: "失败",
  running: "运行中",
} as const;

export function ToolCallGroup({ tools }: ToolCallGroupProps) {
  if (tools.length === 0) {
    return null;
  }
  return (
    <section className="runtime-section" aria-labelledby="runtime-tools-heading">
      <h3 id="runtime-tools-heading">工具调用</h3>
      <ul className="runtime-cards">
        {tools.map((tool) => (
          <li key={tool.id}>
            <header>
              <strong>{tool.agentId} · {tool.name}</strong>
              <span className={`runtime-badge runtime-badge--${tool.status}`}>{statusLabels[tool.status]}</span>
            </header>
            <details>
              <summary>参数、结果与错误</summary>
              <dl className="runtime-payload">
                <div><dt>参数</dt><dd><pre>{formatted(tool.arguments)}</pre></dd></div>
                <div><dt>结果</dt><dd><pre>{formatted(tool.result)}</pre></dd></div>
                <div><dt>错误</dt><dd><pre>{formatted(tool.error)}</pre></dd></div>
              </dl>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}
