import type { ReportingRunView } from "./reporting-store";
import { RunProgress } from "./RunProgress";

export interface RunListProps {
  readonly onSelect: (runId: string) => void;
  readonly runs: readonly ReportingRunView[];
  readonly selectedRunId: string | null;
}

export function RunList({ onSelect, runs, selectedRunId }: RunListProps) {
  return (
    <section className="reporting-run-list" aria-label="报告运行列表">
      <div className="reporting-section-heading">
        <p>RUN INDEX</p>
        <h3>报告运行</h3>
      </div>
      {runs.length === 0 ? <p className="reporting-empty">当前项目还没有报告运行。</p> : null}
      <ul>
        {runs.map((run) => (
          <li key={run.id}>
            <button
              aria-current={selectedRunId === run.id ? "true" : undefined}
              className="reporting-run-list__item"
              onClick={() => onSelect(run.id)}
              type="button"
            >
              <span>
                <strong>{run.title}</strong>
                <small>{run.operation}</small>
              </span>
              <RunProgress run={run} />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
