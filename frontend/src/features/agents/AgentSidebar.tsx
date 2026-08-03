import { DebugPanel } from "../debug/DebugPanel";
import { ToolCallGroup } from "../tools/ToolCallGroup";
import { AgentStatus } from "./AgentStatus";
import type { AgentEventState } from "./event-reducer";
import { QueueView } from "./QueueView";
import { TaskBoard } from "./TaskBoard";
import { ThinkingView } from "./ThinkingView";
import "./agent-sidebar.css";

export interface AgentSidebarProps {
  readonly state: AgentEventState;
}

export function AgentSidebar({ state }: AgentSidebarProps) {
  const agents = Object.values(state.agents).sort((left, right) => (
    left.id === "main" ? -1 : right.id === "main" ? 1 : left.id.localeCompare(right.id)
  ));
  const messages = Object.values(state.messages);
  const notices = state.notices.slice(-5);

  return (
    <div className="agent-sidebar">
      <div className="agent-sidebar__heading">
        <div>
          <p className="pane-label">运行状态</p>
          <h2>Agent 运行态</h2>
        </div>
        <span className={`runtime-freshness ${state.stale ? "runtime-freshness--stale" : ""}`}>
          {state.stale ? "STALE" : "LIVE"}
        </span>
      </div>
      {state.stale ? <p className="runtime-alert" role="alert">运行态可能已过期，正在请求完整快照。</p> : null}

      <ul aria-label="Agent 状态" className="runtime-agents">
        {agents.map((agent) => <AgentStatus agent={agent} key={agent.id} />)}
      </ul>

      <section aria-label="实时回答" className="runtime-section runtime-answer">
        <h3>实时回答</h3>
        {messages.length === 0 ? <p className="pane-muted">当前没有流式输出。</p> : messages.map((message) => (
          <article key={message.id}>
            <strong>{message.agentId}</strong>
            <p>{message.content || "正在生成…"}</p>
            <small>{message.status === "streaming" ? "流式生成中" : "已完成"}</small>
          </article>
        ))}
      </section>

      <ThinkingView messages={messages} />
      <QueueView queues={Object.values(state.queues)} />
      <TaskBoard tasks={Object.values(state.tasks)} />
      <ToolCallGroup tools={Object.values(state.tools)} />

      {state.checkpoints.length > 0 ? (
        <section className="runtime-section" aria-labelledby="runtime-checkpoints-heading">
          <h3 id="runtime-checkpoints-heading">检查点</h3>
          <ul className="runtime-cards">
            {state.checkpoints.map((checkpoint) => (
              <li key={checkpoint.id}>
                <strong>{checkpoint.description || checkpoint.id}</strong>
                <small>{checkpoint.agentId} · epoch {checkpoint.epoch}</small>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {notices.length > 0 ? (
        <section className="runtime-section" aria-labelledby="runtime-notices-heading">
          <h3 id="runtime-notices-heading">系统消息</h3>
          <ul className="runtime-notices">
            {notices.map((notice) => (
              <li className={`runtime-notice runtime-notice--${notice.kind}`} key={notice.id}>
                {notice.content}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <DebugPanel entries={state.debug} usage={state.usage} />
    </div>
  );
}
