import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { createUuid } from "../../app/uuid";
import type { WorkflowApi } from "./workflow-api";
import "./run-workspace.css";

export interface RunWorkspaceProps {
  readonly api: WorkflowApi;
}

function objectValue(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function parseInput(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("运行输入必须是 JSON 对象。");
  }
  return parsed as Record<string, unknown>;
}

export function RunWorkspace({ api }: RunWorkspaceProps) {
  const [workflowId, setWorkflowId] = useState("");
  const [runId, setRunId] = useState("");
  const [inputText, setInputText] = useState('{\n  "instruction": ""\n}');
  const [continuationText, setContinuationText] = useState("{}");
  const [error, setError] = useState<string | null>(null);
  const capabilities = useQuery({
    queryFn: () => api.listCapabilities(),
    queryKey: ["capabilities"],
  });
  const workflows = useQuery({
    queryFn: () => api.listWorkflows(),
    queryKey: ["workflows"],
  });
  const runnable = workflows.data?.workflows.filter((workflow) => workflow.runnable) ?? [];
  const selectedWorkflowId = workflowId || runnable[0]?.id || "";

  const schema = useQuery({
    enabled: Boolean(selectedWorkflowId),
    queryFn: () => api.inputSchema(selectedWorkflowId),
    queryKey: ["workflows", selectedWorkflowId, "input-schema"],
  });
  const run = useQuery({
    enabled: Boolean(runId),
    queryFn: () => api.get(runId),
    queryKey: ["runs", runId],
    refetchInterval: (query) => query.state.data?.run.active ? 1000 : false,
  });
  const outputs = useQuery({
    enabled: Boolean(runId),
    queryFn: () => api.outputs(runId),
    queryKey: ["runs", runId, "outputs"],
  });
  const cost = useQuery({
    enabled: Boolean(runId),
    queryFn: () => api.cost(runId),
    queryKey: ["runs", runId, "cost"],
  });
  const refetchOutputs = outputs.refetch;
  const refetchCost = cost.refetch;
  useEffect(() => {
    if (!runId || run.data === undefined || run.data.run.active) return;
    void refetchOutputs();
    void refetchCost();
  }, [refetchCost, refetchOutputs, run.data, runId]);
  const start = useMutation({
    mutationFn: () => api.start(
      { input: parseInput(inputText), workflowId: selectedWorkflowId },
      createUuid(),
    ),
    onError: (reason) => setError(reason instanceof Error ? reason.message : "启动失败。"),
    onSuccess: (accepted) => {
      setError(null);
      setRunId(accepted.runId);
    },
  });
  const provideInput = useMutation({
    mutationFn: () => {
      const waiting = run.data?.waitingInput[0];
      const inputId = typeof waiting?.decision_id === "string"
        ? waiting.decision_id
        : typeof waiting?.decisionId === "string"
          ? waiting.decisionId
          : undefined;
      return api.provideInput(
        runId,
        { ...(inputId ? { inputId } : {}), values: parseInput(continuationText) },
        createUuid(),
      );
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "提交输入失败。"),
    onSuccess: (accepted) => {
      setError(null);
      setRunId(accepted.runId);
      void run.refetch();
      void outputs.refetch();
      void cost.refetch();
    },
  });

  const totals = objectValue(cost.data?.usage.totals);
  return (
    <section className="run-workspace" aria-label="通用工作流">
      <header><p>CAPABILITY RUNTIME</p><h1>通用工作流</h1></header>
      {capabilities.isPending || workflows.isPending ? <p role="status">正在加载能力定义…</p> : null}
      {capabilities.isError || workflows.isError ? <p role="alert">能力定义加载失败。</p> : null}
      <div className="run-workspace__capabilities">
        {capabilities.data?.capabilities.map((capability) => (
          <article key={capability.id}>
            <strong>{capability.id}</strong>
            <span>{capability.description}</span>
            <small>v{capability.version}</small>
          </article>
        ))}
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (selectedWorkflowId && !start.isPending) start.mutate();
        }}
      >
        <label>工作流<select onChange={(event) => setWorkflowId(event.target.value)} value={selectedWorkflowId}>
          {runnable.map((workflow) => <option key={workflow.id}>{workflow.id}</option>)}
        </select></label>
        <label>运行输入 JSON<textarea
          aria-label="运行输入 JSON"
          onChange={(event) => setInputText(event.target.value)}
          value={inputText}
        /></label>
        {schema.data ? <details><summary>输入 Schema</summary><pre>{JSON.stringify(schema.data.schema, null, 2)}</pre></details> : null}
        <button disabled={!selectedWorkflowId || start.isPending} type="submit">启动工作流</button>
      </form>
      {error ? <p role="alert">{error}</p> : null}
      {run.data ? <section className="run-workspace__run" aria-label="运行结果">
        <h2>{run.data.run.runId}</h2>
        <dl>
          <div><dt>Capability</dt><dd>{run.data.run.capabilityId}</dd></div>
          <div><dt>Workflow</dt><dd>{run.data.run.workflowId}</dd></div>
          <div><dt>状态</dt><dd>{run.data.run.status}</dd></div>
        </dl>
        <h3>Outputs</h3>
        <ul>{outputs.data?.outputs.map((output) => (
          <li key={output.id}>
            <span>{output.path ?? JSON.stringify(output.value)}</span>
            {typeof output.size === "number" ? <small>{output.size} B</small> : null}
          </li>
        ))}</ul>
        <h3>Cost</h3>
        <p>{String(totals.total_tokens ?? 0)} tokens</p>
        <p>{String(totals.estimated_cost ?? 0)}</p>
        {!run.data.run.active && (
          run.data.run.capabilityId === "distribution-reporting"
          || run.data.waitingInput.length > 0
        ) ? <form onSubmit={(event) => { event.preventDefault(); provideInput.mutate(); }}>
          <label>继续输入 JSON<textarea onChange={(event) => setContinuationText(event.target.value)} value={continuationText} /></label>
          <button disabled={provideInput.isPending} type="submit">提交运行输入</button>
        </form> : null}
      </section> : null}
    </section>
  );
}
