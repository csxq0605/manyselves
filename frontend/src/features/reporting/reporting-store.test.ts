import { describe, expect, it } from "vitest";

import type { RuntimeEvent } from "../../api/event-stream";
import {
  createReportingStore,
  normalizeReportingSnapshot,
  normalizeRun,
} from "./reporting-store";

function event(
  sequence: number,
  type: string,
  payload: Record<string, unknown>,
): RuntimeEvent {
  return {
    eventId: `stream-1:evt-${sequence}`,
    payload,
    schemaVersion: 1,
    sequence,
    streamId: "stream-1",
    timestamp: `2026-08-03T08:00:${String(sequence).padStart(2, "0")}Z`,
    type,
  };
}

describe("reporting read model", () => {
  it.each([
    ["planning", "planning"],
    ["in_progress", "running"],
    ["needs_user_decision", "waiting_user"],
    ["waiting_user", "waiting_user"],
    ["revision-review", "revising"],
    ["completed", "completed"],
    ["failed", "failed"],
    ["cancelled", "cancelled"],
  ] as const)("normalizes persisted status %s to %s", (status, expected) => {
    expect(normalizeRun({ run_id: "run-1", status }).status).toBe(expected);
  });

  it("does not present a completed run when its verified output is missing", () => {
    const snapshot = normalizeReportingSnapshot({
      checkpoint: { activity: "delivery", status: "completed" },
      evidence: {},
      outputs: [{ exists: false, path: "Outputs/Reports/report.docx", sha256: null, size: 0 }],
      revision: {},
      run: { active: false, run_id: "run-1", status: "completed" },
      state: { activity: "delivery", status: "completed" },
      waitingInput: [],
    });

    expect(snapshot.run.status).toBe("failed");
    expect(snapshot.verification).toEqual({
      message: "输出文件缺失或未通过服务端校验",
      status: "failed",
    });
  });

  it("hydrates a durable snapshot before replaying later buffered reporting events", () => {
    const store = createReportingStore("stream-1");
    store.getState().beginSnapshot("run-1");
    store.getState().applyEvent(event(10, "report.status.changed", {
      content: "模块分析已完成",
      report_type: "progress",
      run_id: "run-1",
      status: "running",
      task_id: "module-2.1",
    }));
    store.getState().applyEvent(event(11, "reporting.agent_result.changed", {
      result_path: "Work/runs/run-1/results/module-2.1.json",
      run_id: "run-1",
      sender: "module-2.1-specialist",
      status: "completed",
      task_id: "module-2.1",
    }));

    store.getState().hydrateSnapshot("run-1", {
      checkpoint: { activity: "dispatch", completed_modules: [], status: "in_progress" },
      evidence: {},
      outputs: [],
      revision: {},
      run: { active: true, run_id: "run-1", status: "running" },
      state: { activity: "dispatch", status: "in_progress" },
      waitingInput: [],
    });

    const snapshot = store.getState().snapshots["run-1"];
    expect(snapshot?.timeline.map((item) => item.summary)).toEqual([
      "模块分析已完成",
      "module-2.1-specialist · completed",
    ]);
    expect(snapshot?.agents["module-2.1-specialist"]?.status).toBe("completed");
    expect(store.getState().pendingSnapshots.has("run-1")).toBe(false);
  });

  it("deduplicates events and requests a snapshot refresh after a sequence gap", () => {
    const store = createReportingStore("stream-1");
    store.getState().applyEvent(event(20, "reporting.progress.changed", {
      note_kind: "progress",
      run_id: "run-1",
      sender: "analyst",
      task_id: "research",
    }));
    store.getState().applyEvent(event(20, "reporting.progress.changed", {
      note_kind: "progress",
      run_id: "run-1",
      sender: "analyst",
      task_id: "research",
    }));
    store.getState().applyEvent(event(22, "reporting.blocked", {
      reason: "缺少计量数据",
      run_id: "run-1",
      sender: "analyst",
      task_id: "research",
    }));

    expect(store.getState().snapshots["run-1"]?.timeline).toHaveLength(1);
    expect(store.getState().refreshRequested).toBe(true);
    expect(store.getState().stale).toBe(true);
  });

  it("derives resumability only from server-supported persisted states", () => {
    expect(normalizeRun({ active: false, run_id: "failed", status: "failed" }).canResume).toBe(true);
    expect(normalizeRun({ active: false, run_id: "waiting", status: "needs_user_decision" }).canResume).toBe(false);
    expect(normalizeRun({ active: false, run_id: "done", status: "completed" }).canResume).toBe(false);
  });

  it("projects dynamic specialist and checkpoint events onto the selected reporting run", () => {
    const store = createReportingStore("stream-1");
    store.getState().selectRun("run-1");
    store.getState().hydrateSnapshot("run-1", {
      checkpoint: { activity: "dispatch", run_id: "run-1", status: "in_progress" },
      evidence: {}, outputs: [], revision: {},
      run: { active: true, run_id: "run-1", status: "running" },
      state: { activity: "dispatch", status: "in_progress" }, waitingInput: [],
    });
    store.getState().applyEvent(event(1, "agent.status.changed", {
      agent_type: "module-2.5-specialist",
      status: "running",
    }));
    store.getState().applyEvent(event(2, "checkpoint.created", {
      checkpoint_id: "checkpoint-2",
      description: "module 2.5 completed",
    }));

    const current = store.getState().snapshots["run-1"];
    expect(current?.agents["module-2.5-specialist"]?.status).toBe("running");
    expect(current?.checkpoint.checkpoint_id).toBe("checkpoint-2");
  });
});
