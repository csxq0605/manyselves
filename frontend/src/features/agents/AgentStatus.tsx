import type { RuntimeAgentView } from "./event-reducer";

export interface AgentStatusProps {
  readonly agent: RuntimeAgentView;
}

const statusLabels: Readonly<Record<string, string>> = {
  error: "错误",
  idle: "空闲",
  running: "运行中",
  thinking: "思考中",
  waiting: "等待中",
};

export function AgentStatus({ agent }: AgentStatusProps) {
  return (
    <li className="runtime-agent">
      <span aria-hidden="true" className={`runtime-agent__signal runtime-agent__signal--${agent.status}`} />
      <span>
        <strong>{agent.id}</strong>
        <small>{statusLabels[agent.status] ?? agent.status}</small>
      </span>
    </li>
  );
}
