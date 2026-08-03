import type { RuntimeTaskView } from "./event-reducer";

export interface TaskBoardProps {
  readonly tasks: readonly RuntimeTaskView[];
}

export function TaskBoard({ tasks }: TaskBoardProps) {
  if (tasks.length === 0) {
    return null;
  }
  return (
    <section className="runtime-section" aria-labelledby="runtime-tasks-heading">
      <h3 id="runtime-tasks-heading">任务</h3>
      <ul className="runtime-cards">
        {tasks.map((task) => (
          <li key={task.id}>
            <span className={`runtime-badge runtime-badge--${task.status}`}>{task.status}</span>
            <strong>{task.brief || task.id}</strong>
            <small>{task.sourceAgent} → {task.targetAgent}{task.blocking ? " · 阻塞" : ""}</small>
          </li>
        ))}
      </ul>
    </section>
  );
}
