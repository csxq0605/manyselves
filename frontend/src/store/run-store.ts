import { create } from "zustand";
import { persist } from "zustand/middleware";

interface RunState {
  currentRunIds: Record<string, string>;
  getCurrentRun: (projectId: string) => string | null;
  setCurrentRun: (projectId: string, runId: string | null) => void;
}

export const useRunStore = create<RunState>()(
  persist(
    (set, get) => ({
      currentRunIds: {},
      getCurrentRun: (projectId) => get().currentRunIds[projectId] ?? null,
      setCurrentRun: (projectId, runId) => set((state) => {
        const currentRunIds = { ...state.currentRunIds };
        if (runId) currentRunIds[projectId] = runId;
        else delete currentRunIds[projectId];
        return { currentRunIds };
      }),
    }),
    { name: "manyselves-current-run", version: 1 },
  ),
);
