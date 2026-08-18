export interface ChatMessage {
  readonly checkpointId: string | null;
  readonly content: string;
  readonly id: string;
  readonly messageId: string | null;
  readonly role: string;
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
      timestamp: stringValue(record.ts) ?? stringValue(record.timestamp),
    };
  });
}
