import { describe, expect, it } from "vitest";

import { createConversationMessageStore } from "./message-store";

describe("conversation message store", () => {
  it("keeps unsent drafts isolated by session", () => {
    const store = createConversationMessageStore();

    store.getState().setDraft("session-a", "first draft");
    store.getState().setDraft("session-b", "second draft");

    expect(store.getState().drafts).toEqual({
      "session-a": "first draft",
      "session-b": "second draft",
    });
    store.getState().clearDraft("session-a");
    expect(store.getState().drafts["session-a"]).toBeUndefined();
    expect(store.getState().drafts["session-b"]).toBe("second draft");
  });
});
