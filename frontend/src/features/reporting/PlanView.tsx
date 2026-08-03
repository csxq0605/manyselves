import { asRecord, asStringArray, asText, type ReportingSnapshotView } from "./reporting-store";

const modules = ["2.1", "2.2", "2.3", "2.4", "2.5"] as const;

export function PlanView({ snapshot }: { readonly snapshot: ReportingSnapshotView }) {
  const completed = new Set(asStringArray(snapshot.checkpoint.completed_modules));
  const specialists = new Set(asStringArray(snapshot.checkpoint.specialist_modules));
  const reviewRefs = asRecord(snapshot.checkpoint.module_review_completion_refs);
  return (
    <section className="reporting-card" aria-label="报告计划与模块">
      <div className="reporting-section-heading">
        <p>EXECUTION PLAN</p>
        <h3>计划、模块与专业智能体</h3>
      </div>
      <p className="reporting-meta">
        当前阶段：{asText(snapshot.state.activity, "等待启动")} · {snapshot.run.operation}
      </p>
      <ol aria-label="报告模块" className="reporting-module-grid">
        {modules.map((moduleId) => {
          const status = completed.has(moduleId)
            ? "已完成"
            : specialists.has(moduleId) ? "处理中" : "待处理";
          return (
            <li key={moduleId}>
              <strong>{moduleId}</strong>
              <span>{status}</span>
              {reviewRefs[moduleId] ? <small>已完成模块审阅</small> : null}
            </li>
          );
        })}
      </ol>
      <div className="reporting-agent-grid">
        {Object.values(snapshot.agents).map((agent) => (
          <article key={agent.id}>
            <strong>{agent.id}</strong>
            <span>{agent.status}</span>
            {agent.taskId ? <small>{agent.taskId}</small> : null}
          </article>
        ))}
      </div>
      {snapshot.timeline.length > 0 ? (
        <ol className="reporting-timeline">
          {snapshot.timeline.map((item) => (
            <li key={item.id}><strong>{item.agentId}</strong><span>{item.summary}</span></li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
