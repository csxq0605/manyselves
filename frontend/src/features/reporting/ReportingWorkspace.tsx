import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useStore } from "zustand";

import type { PlatformBridge } from "../../platform/types";
import type {
  ReportingApi,
  ReportingDecisionRequest,
  ReportingResumeRequest,
  ReportingRevisionRequest,
  ReportingStartRequest,
} from "./reporting-api";
import type { ReportingStore } from "./reporting-store";
import { EvidenceView } from "./EvidenceView";
import { OutputArtifacts } from "./OutputArtifacts";
import { PlanView } from "./PlanView";
import { RevisionView } from "./RevisionView";
import { RunList } from "./RunList";
import { RunProgress } from "./RunProgress";
import { WaitingInputForm } from "./WaitingInputForm";
import "./reporting.css";

const reportModuleIds = ["2.1", "2.2", "2.3", "2.4", "2.5"] as const;

export interface ReportingWorkspaceProps {
  readonly api: ReportingApi;
  readonly platform: PlatformBridge;
  readonly projectId: string;
  readonly store: ReportingStore;
}

function commandId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `report-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function outputName(path: string): string {
  return path.split("/").at(-1) || "report-output";
}

export function ReportingWorkspace({ api, platform, projectId, store }: ReportingWorkspaceProps) {
  const queryClient = useQueryClient();
  const state = useStore(store);
  const [instruction, setInstruction] = useState("");
  const [operation, setOperation] = useState<ReportingStartRequest["operation"]>("full_report");
  const [targetModule, setTargetModule] = useState<(typeof reportModuleIds)[number]>("2.1");
  const [sourceMarkdownRef, setSourceMarkdownRef] = useState("Inputs/report.md");
  const [resumeSupplement, setResumeSupplement] = useState("");
  const [commandError, setCommandError] = useState<string | null>(null);
  const commandKeys = useRef(new Map<string, string>());

  function semanticKey(kind: string, payload: unknown): string {
    return `${kind}:${JSON.stringify(payload)}`;
  }

  function idempotencyKey(kind: string, payload: unknown): string {
    const semantic = semanticKey(kind, payload);
    const existing = commandKeys.current.get(semantic);
    if (existing) return existing;
    const created = commandId();
    commandKeys.current.set(semantic, created);
    if (commandKeys.current.size > 64) {
      const oldest = commandKeys.current.keys().next().value;
      if (oldest) commandKeys.current.delete(oldest);
    }
    return created;
  }

  function releaseKey(kind: string, payload: unknown) {
    commandKeys.current.delete(semanticKey(kind, payload));
  }

  const runsQuery = useQuery({
    queryFn: async () => {
      const response = await api.list();
      store.getState().hydrateRuns(response.runs);
      return response;
    },
    queryKey: ["reporting", "runs", projectId],
  });

  useEffect(() => {
    if (!state.selectedRunId && state.runs[0]) {
      store.getState().selectRun(state.runs[0].id);
    }
  }, [state.runs, state.selectedRunId, store]);

  const runId = state.selectedRunId;
  const snapshotQuery = useQuery({
    enabled: Boolean(runId),
    queryFn: async () => {
      if (!runId) throw new Error("Reporting run is not selected");
      store.getState().beginSnapshot(runId);
      try {
        const response = await api.get(runId);
        store.getState().hydrateSnapshot(runId, response);
        return response;
      } catch (error) {
        store.getState().failSnapshot(runId);
        throw error;
      }
    },
    queryKey: ["reporting", "run", projectId, runId],
  });
  const snapshot = runId ? state.snapshots[runId] : undefined;

  async function refresh(run = runId) {
    await queryClient.invalidateQueries({ queryKey: ["reporting", "runs", projectId] });
    if (run) {
      await queryClient.invalidateQueries({ queryKey: ["reporting", "run", projectId, run] });
    }
  }

  const startMutation = useMutation({
    mutationFn: (input: ReportingStartRequest) => api.start(input, idempotencyKey("start", input)),
    onError: () => setCommandError("创建报告运行失败。"),
    onSuccess: async (accepted, input) => {
      releaseKey("start", input);
      setCommandError(null);
      setInstruction("");
      store.getState().selectRun(accepted.runId);
      await refresh(accepted.runId);
    },
  });
  const resumeMutation = useMutation({
    mutationFn: () => {
      const input: ReportingResumeRequest = {
        supplements: resumeSupplement.trim()
        ? [{ content: resumeSupplement.trim(), scope: "run" }]
        : [],
      };
      const semantic = { input, runId: runId ?? "" };
      return api.resume(runId ?? "", input, idempotencyKey("resume", semantic));
    },
    onError: () => setCommandError("当前运行不能从已保存检查点恢复。"),
    onSuccess: async () => {
      const input: ReportingResumeRequest = {
        supplements: resumeSupplement.trim()
          ? [{ content: resumeSupplement.trim(), scope: "run" }]
          : [],
      };
      releaseKey("resume", { input, runId: runId ?? "" });
      setCommandError(null);
      setResumeSupplement("");
      await refresh();
    },
  });
  const cancelMutation = useMutation({
    mutationFn: () => api.cancel(
      runId ?? "",
      idempotencyKey("cancel", { runId: runId ?? "" }),
    ),
    onError: () => setCommandError("取消失败，运行状态可能已经改变。"),
    onSuccess: () => {
      releaseKey("cancel", { runId: runId ?? "" });
      return refresh();
    },
  });

  async function decide(decisionId: string, input: ReportingDecisionRequest) {
    const semantic = { decisionId, input };
    await api.decide(decisionId, input, idempotencyKey("decision", semantic));
    releaseKey("decision", semantic);
    await refresh();
  }

  async function revise(input: ReportingRevisionRequest) {
    const accepted = await api.revise(input, idempotencyKey("revision", input));
    releaseKey("revision", input);
    store.getState().selectRun(accepted.runId);
    await refresh(accepted.runId);
  }

  async function download(path: string) {
    const blob = await api.download(projectId, path);
    await platform.saveDownload({ blob, suggestedName: outputName(path) });
  }

  return (
    <section className="reporting-workspace" aria-label="报告工作区">
      <aside className="reporting-workspace__rail">
        <form
          className="reporting-create"
          onSubmit={(event) => {
            event.preventDefault();
            if (!instruction.trim() || startMutation.isPending) return;
            startMutation.mutate({
              instruction: instruction.trim(),
              maxProviderAttempts: 80,
              maxTotalTokens: 800_000,
              missingEvidencePolicy: "ask",
              operation,
              ...(operation === "module_report" ? { targetModules: [targetModule] } : {}),
              ...(operation === "aggregate_existing" ? {
                sourceModuleRefs: Object.fromEntries(
                  reportModuleIds.map((moduleId) => [moduleId, `Outputs/Modules/${moduleId}.md`]),
                ),
                targetModules: [...reportModuleIds],
              } : {}),
              ...(operation === "render_existing" ? { sourceMarkdownRef } : {}),
              ...(operation === "distill_template_skill" ? { targetModules: [] } : {}),
            });
          }}
        >
          <div className="reporting-section-heading">
            <p>NEW WORKFLOW</p>
            <h3>创建报告</h3>
          </div>
          <label>
            <span>运行类型</span>
            <select value={operation} onChange={(event) => setOperation(event.target.value as typeof operation)}>
              <option value="full_report">完整报告</option>
              <option value="module_report">模块报告</option>
              <option value="aggregate_existing">汇总既有模块</option>
              <option value="render_existing">渲染既有 Markdown</option>
              <option value="distill_template_skill">蒸馏模板 Skill</option>
            </select>
          </label>
          {operation === "module_report" ? (
            <label>
              <span>目标模块</span>
              <select value={targetModule} onChange={(event) => setTargetModule(event.target.value as typeof targetModule)}>
                {reportModuleIds.map((moduleId) => <option key={moduleId}>{moduleId}</option>)}
              </select>
            </label>
          ) : null}
          {operation === "aggregate_existing" ? (
            <p className="reporting-meta">将读取 Outputs/Modules/2.1.md 至 2.5.md。</p>
          ) : null}
          {operation === "render_existing" ? (
            <label>
              <span>来源 Markdown</span>
              <input value={sourceMarkdownRef} onChange={(event) => setSourceMarkdownRef(event.target.value)} />
            </label>
          ) : null}
          <label>
            <span>报告要求</span>
            <textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} />
          </label>
          <button disabled={!instruction.trim() || startMutation.isPending} type="submit">
            {startMutation.isPending ? "创建中…" : "开始报告运行"}
          </button>
        </form>
        <RunList
          onSelect={(selected) => store.getState().selectRun(selected)}
          runs={state.runs}
          selectedRunId={runId}
        />
      </aside>

      <div className="reporting-workspace__content">
        {runsQuery.isPending ? <p role="status">正在读取报告运行…</p> : null}
        {runsQuery.isError ? <p role="alert">报告运行列表加载失败。</p> : null}
        {state.stale ? <p role="alert">实时事件不连续，正在等待服务端快照重新同步。</p> : null}
        {snapshotQuery.isPending && runId ? <p role="status">正在加载报告快照…</p> : null}
        {snapshotQuery.isError ? <p role="alert">报告快照加载失败。</p> : null}
        {commandError ? <p role="alert">{commandError}</p> : null}
        {snapshot ? (
          <>
            <header className="reporting-workspace__header">
              <div>
                <p>REPORT CONTROL ROOM</p>
                <h2>报告运行 {snapshot.run.id}</h2>
              </div>
              <RunProgress run={snapshot.run} />
              <div className="reporting-workspace__commands">
                {snapshot.run.canCancel ? (
                  <button
                    disabled={cancelMutation.isPending}
                    onClick={() => cancelMutation.mutate()}
                    type="button"
                  >取消运行</button>
                ) : null}
                {snapshot.run.canResume ? (
                  <button
                    disabled={resumeMutation.isPending}
                    onClick={() => resumeMutation.mutate()}
                    type="button"
                  >从检查点恢复 / 重试</button>
                ) : null}
              </div>
            </header>
            {snapshot.run.canResume ? (
              <label className="reporting-resume-supplement">
                <span>恢复时补充信息（可选）</span>
                <textarea value={resumeSupplement} onChange={(event) => setResumeSupplement(event.target.value)} />
              </label>
            ) : null}
            {snapshot.waitingInput.map((request) => (
              <WaitingInputForm key={request.decisionId} onSubmit={decide} request={request} />
            ))}
            <div className="reporting-dashboard-grid">
              <PlanView snapshot={snapshot} />
              <EvidenceView snapshot={snapshot} />
              <RevisionView onSubmit={revise} snapshot={snapshot} />
              <OutputArtifacts onDownload={download} snapshot={snapshot} />
            </div>
          </>
        ) : null}
      </div>
    </section>
  );
}
