export interface ChatMessage {
  readonly checkpointId: string | null;
  readonly content: string;
  readonly id: string;
  readonly messageId: string | null;
  readonly role: string;
  /** Who sent a user turn: "user" for a person, "system" for a notice sent to the Agent. */
  readonly source: string | null;
  readonly timestamp: string | null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function normalizeMessages(records: readonly Record<string, unknown>[]): ChatMessage[] {
  return records.map((record, index) => {
    const role = stringValue(record.role) ?? "system";
    const messageId = stringValue(record.message_id) ?? stringValue(record.messageId);
    const checkpointId = stringValue(record.checkpoint_id) ?? stringValue(record.checkpointId);
    return {
      checkpointId,
      content: String(record.content ?? ""),
      id: `${messageId ?? checkpointId ?? "record"}-${index}`,
      messageId,
      role: role === "agent" ? "assistant" : role,
      source: stringValue(record.source),
      timestamp: stringValue(record.ts) ?? stringValue(record.timestamp),
    };
  });
}

/** A notice the system sent to the Agent as a user turn: the Agent reads it, people do not. */
export function isSystemNotice(message: ChatMessage): boolean {
  return message.role === "user" && message.source === "system";
}
