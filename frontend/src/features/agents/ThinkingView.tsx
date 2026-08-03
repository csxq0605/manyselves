import type { RuntimeMessageView } from "./event-reducer";

export interface ThinkingViewProps {
  readonly messages: readonly RuntimeMessageView[];
}

export function ThinkingView({ messages }: ThinkingViewProps) {
  const thinking = messages.filter((message) => message.thinking);
  if (thinking.length === 0) {
    return null;
  }
  return (
    <section aria-label="Thinking" className="runtime-section runtime-thinking">
      <h3>Thinking</h3>
      {thinking.map((message) => (
        <details key={message.id}>
          <summary>{message.agentId} 的过程摘要</summary>
          <p>{message.thinking}</p>
        </details>
      ))}
    </section>
  );
}
