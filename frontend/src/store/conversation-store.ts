import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * Global conversation state management.
 * Persists the active conversation ID across page navigation.
 */

interface ConversationState {
  // Per-project active session ID mapping
  activeSessionIds: Record<string, string>;
  // Set active session for a project
  setActiveSession: (projectId: string, sessionId: string | null) => void;
  // Get active session for a project
  getActiveSession: (projectId: string) => string | null;
  // Clear active session for a project
  clearActiveSession: (projectId: string) => void;
}

export const useConversationStore = create<ConversationState>()(
  persist(
    (set, get) => ({
      activeSessionIds: {},

      setActiveSession: (projectId, sessionId) => {
        set((state) => {
          const updated = { ...state.activeSessionIds };
          if (sessionId) {
            updated[projectId] = sessionId;
          } else {
            delete updated[projectId];
          }
          return { activeSessionIds: updated };
        });
      },

      getActiveSession: (projectId) => {
        return get().activeSessionIds[projectId] ?? null;
      },

      clearActiveSession: (projectId) => {
        set((state) => {
          const updated = { ...state.activeSessionIds };
          delete updated[projectId];
          return { activeSessionIds: updated };
        });
      },
    }),
    {
      name: "manyselves-active-conversation",
      version: 1,
    }
  )
);