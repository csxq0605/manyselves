import type { RuntimeQueueView } from "./event-reducer";

export interface QueueViewProps {
  readonly queues: readonly RuntimeQueueView[];
}

export function QueueView({ queues }: QueueViewProps) {
  if (queues.every((queue) => queue.pendingCount === 0)) {
    return null;
  }
  return (
    <section className="runtime-section" aria-labelledby="runtime-queues-heading">
      <h3 id="runtime-queues-heading">等待队列</h3>
      {queues.filter((queue) => queue.pendingCount > 0).map((queue) => (
        <div className="runtime-queue" key={queue.agentId}>
          <strong>{queue.agentId} · {queue.pendingCount}</strong>
          <ol>
            {queue.items.map((item, index) => <li key={`${queue.agentId}-${index}`}>{item}</li>)}
          </ol>
        </div>
      ))}
    </section>
  );
}
