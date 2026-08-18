import { createStore, type StoreApi } from "zustand/vanilla";

export interface ConversationMessageState {
  readonly drafts: Readonly<Record<string, string>>;
  clearDraft(sessionId: string): void;
  setDraft(sessionId: string, content: string): void;
}

export type ConversationMessageStore = StoreApi<ConversationMessageState>;

export function createConversationMessageStore(): ConversationMessageStore {
  return createStore<ConversationMessageState>((set) => ({
    drafts: {},
    clearDraft: (sessionId) => set((state) => {
      const drafts = { ...state.drafts };
      delete drafts[sessionId];
      return { drafts };
    }),
    setDraft: (sessionId, content) => set((state) => ({
      drafts: { ...state.drafts, [sessionId]: content },
    })),
  }));
}
