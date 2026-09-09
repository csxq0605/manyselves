import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { createUuid } from "../../app/uuid";
import { useRunStore } from "../../store/run-store";
import {
  reportingActionLabel,
  reportingRunStage,
  reportingRunStepSummary,
  reportingWorkflowLabel,
} from "../reporting/reporting-run-presentation";
import type { WorkflowApi, WorkflowRunFeedApi, WorkflowRunResponse } from "./workflow-api";
import "./run-interaction-feed.css";

type JsonSchema = {
  readonly default?: unknown;
  readonly description?: string;
  readonly enum?: readonly unknown[];
  readonly properties?: Record<string, JsonSchema>;
  readonly required?: readonly string[];
  readonly title?: string;
  readonly type?: string;
  readonly [key: `x-${string}`]: unknown;
};

type WaitingInput = Record<string, unknown> & {
  readonly description?: string;
  readonly path?: readonly unknown[];
  readonly schema?: JsonSchema;
  readonly title?: string;
};

function objectValue(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function waitingInputId(waiting: WaitingInput): string | undefined {
  const inputId = waiting.input_id ?? waiting.inputId ?? waiting.decision_id ?? waiting.decisionId;
  return typeof inputId === "string" ? inputId : undefined;
}

function branchLabel(waiting: WaitingInput): string | null {
  if (!Array.isArray(waiting.path)) return null;
  for (const segment of [...waiting.path].reverse()) {
    const item = objectValue(segment);
    const branchId = item.branch_id ?? item.branchId;
    if (typeof branchId === "string" && branchId.length > 0) return `模块 ${branchId}`;
  }
  return null;
}

function enumLabel(schema: JsonSchema, option: unknown): string {
  const labels = objectValue(schema["x-enum-labels"]);
  const label = labels[String(option)];
  return typeof label === "string" ? label : String(option);
}

function initialValues(schema: JsonSchema): Record<string, unknown> {
  return Object.fromEntries(Object.entries(schema.properties ?? {}).flatMap(([name, property]) => (
    property.default === undefined ? [] : [[name, property.default]]
  )));
}

interface WaitingInteractionProps {
  readonly api: WorkflowApi;
  readonly run: WorkflowRunResponse;
  readonly waiting: WaitingInput;
}

function WaitingInteraction({ api, run, waiting }: WaitingInteractionProps) {
  const client = useQueryClient();
  const schema = waiting.schema ?? {};
  const [values, setValues] = useState<Record<string, unknown>>(() => initialValues(schema));
  const [jsonValue, setJsonValue] = useState("{}");
  const [error, setError] = useState<string | null>(null);
  const properties = schema.properties;
  const hasControls = properties !== undefined && Object.keys(properties).length > 0;
  const mutation = useMutation({
    mutationFn: () => {
      const inputId = waitingInputId(waiting);
      return api.provideInput(run.run.runId, {
        ...(inputId ? { inputId } : {}),
        values: hasControls ? values : JSON.parse(jsonValue) as unknown,
      }, createUuid());
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "提交输入失败。"),
    onSuccess: () => {
      setError(null);
      void client.invalidateQueries({ queryKey: ["runs", "interaction-feed"] });
    },
  });

  return (
    <article className="run-interaction-card">
      <div className="run-interaction-card__context">
        <span>原运行 · {run.run.runId}</span>
        {branchLabel(waiting) ? <strong>{branchLabel(waiting)}</strong> : null}
      </div>
      <h2>{waiting.title ?? schema.title ?? "需要你的输入"}</h2>
      {waiting.description ?? schema.description
        ? <p>{waiting.description ?? schema.description}</p>
        : null}
      <form onSubmit={(event) => {
        event.preventDefault();
        setError(null);
        mutation.mutate();
      }}>
        {hasControls ? Object.entries(properties).map(([name, property]) => {
          const label = property.title ?? name;
          const required = schema.required?.includes(name) ?? false;
          if (property.enum && property.enum.length > 0) {
            return (
              <fieldset key={name}>
                <legend>{label}{required ? " *" : ""}</legend>
                {property.enum.map((option) => <label className="run-interaction-card__choice" key={String(option)}>
                  <input
                    checked={values[name] === option}
                    name={`${run.run.runId}-${name}`}
                    onChange={() => setValues((current) => ({ ...current, [name]: option }))}
                    required={required}
                    type="radio"
                  />
                  <span>{enumLabel(property, option)}</span>
                </label>)}
                {property.description ? <small>{property.description}</small> : null}
              </fieldset>
            );
          }
          if (property.type === "boolean") {
            return <label className="run-interaction-card__choice" key={name}>
              <input
                aria-label={label}
                checked={values[name] === true}
                onChange={(event) => setValues((current) => ({ ...current, [name]: event.currentTarget.checked }))}
                type="checkbox"
              />
              <span>{label}</span>
            </label>;
          }
          if (["integer", "number", "string"].includes(property.type ?? "")) {
            return <label key={name}>
              <span>{label}{required ? " *" : ""}</span>
              {property.type === "string" && name.toLowerCase().includes("rationale") ? (
                <textarea
                  aria-label={label}
                  onChange={(event) => {
                    const value = event.currentTarget.value;
                    setValues((current) => ({ ...current, [name]: value }));
                  }}
                  required={required}
                  rows={3}
                  value={String(values[name] ?? "")}
                />
              ) : (
                <input
                  aria-label={label}
                  onChange={(event) => {
                    const value = event.currentTarget.value;
                    setValues((current) => ({
                      ...current,
                      [name]: property.type === "string" ? value : Number(value),
                    }));
                  }}
                  required={required}
                  step={property.type === "integer" ? 1 : property.type === "number" ? "any" : undefined}
                  type={property.type === "string" ? "text" : "number"}
                  value={String(values[name] ?? "")}
                />
              )}
              {property.description ? <small>{property.description}</small> : null}
            </label>;
          }
          return <label key={name}>
            <span>{label}{required ? " *" : ""}</span>
            <textarea
              aria-label={label}
              onChange={(event) => {
                const value = event.currentTarget.value;
                setValues((current) => {
                try {
                  return { ...current, [name]: JSON.parse(value) as unknown };
                } catch {
                  return current;
                }
                });
              }}
              required={required}
              rows={3}
            />
          </label>;
        }) : <label>
          <span>输入 JSON</span>
          <textarea aria-label="输入 JSON" onChange={(event) => setJsonValue(event.currentTarget.value)} rows={4} value={jsonValue} />
        </label>}
        {error ? <p role="alert">{error}</p> : null}
        <button disabled={mutation.isPending} type="submit">
          {mutation.isPending ? "正在提交…" : "提交并继续原运行"}
        </button>
      </form>
    </article>
  );
}

function isInterrupted(run: WorkflowRunResponse): boolean {
  return !run.run.active
    && run.waitingInput.length === 0
    && ["failed", "running"].includes(run.run.status.toLowerCase());
}

function stateString(run: WorkflowRunResponse, key: string): string | null {
  const value = run.state[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function failureMessage(run: WorkflowRunResponse): string | null {
  const error = stateString(run, "error")
    ?? stateString(run, "error_message")
    ?? stateString(run, "message");
  if (!error) return null;
  const embeddedMessage = /\bmessage='([^']+)'\s+details=/.exec(error)?.[1];
  return embeddedMessage ?? error;
}

function RunResult({ api, run, projectId }: {
  readonly api: WorkflowApi;
  readonly run: WorkflowRunResponse;
  readonly projectId: string;
}) {
  const cost = useQuery({
    queryKey: ["runs", "cost", projectId, run.run.runId, run.run.status],
    queryFn: () => api.cost(run.run.runId),
  });
  const outputs = useQuery({
    queryKey: ["runs", "outputs", projectId, run.run.runId],
    queryFn: () => api.outputs(run.run.runId),
  });
  const usage = objectValue(cost.data?.usage);
  const totals = objectValue(usage.totals);
  return <article className="run-interaction-card run-interaction-card--result" aria-live="polite">
    <strong>{reportingWorkflowLabel(run.run.workflowId)}已完成</strong>
    <small>{run.run.runId}</small>
    {outputs.data?.outputs.length ? <details>
      <summary>本次运行记录的输出路径</summary>
      <small>Outputs 是项目当前发布位置，后续运行可能更新；历史版本以各 Run 的交付包为准。</small>
      {outputs.data.outputs.map((output) => <div key={output.id}>
        {output.path}{output.exists === false ? "（文件当前不可用）" : ""}
      </div>)}
    </details> : null}
    {outputs.isError ? <p role="alert">交付文件列表加载失败，请刷新重试。</p> : null}
    {cost.isPending ? <span>正在读取本次运行成本…</span> : null}
    {cost.isError ? <p role="alert">成本读取失败，请刷新重试。</p> : null}
    {typeof totals.total_tokens === "number" ? <span>
      累计 Token：{totals.total_tokens.toLocaleString()} · Provider 调用：{String(totals.provider_attempts ?? "未知")}
    </span> : null}
    {typeof usage.pricing_summary === "string"
      ? <p className="run-interaction-card__cost">{usage.pricing_summary}</p>
      : cost.isSuccess ? <p>成本未知：当前模型未配置价格；Token 用量不等于实际账单。</p> : null}
    <a href={`/projects/${encodeURIComponent(projectId)}/outputs`}>查看交付文件</a>
  </article>;
}

export interface RunInteractionFeedProps {
  readonly api: WorkflowApi & WorkflowRunFeedApi;
  readonly conversationId: string;
  readonly projectId: string;
}

export function RunInteractionFeed({ api, conversationId, projectId }: RunInteractionFeedProps) {
  const client = useQueryClient();
  const setCurrentRun = useRunStore((state) => state.setCurrentRun);
  const runs = useQuery({
    queryFn: () => api.listRuns(conversationId),
    queryKey: ["runs", "interaction-feed", conversationId],
    refetchInterval: 5_000,
  });
  const resume = useMutation({
    mutationFn: (runId: string) => api.resume(runId, createUuid()),
    onSuccess: (accepted) => {
      setCurrentRun(projectId, accepted.runId);
      void client.invalidateQueries({ queryKey: ["runs", "interaction-feed"] });
    },
  });
  const feedRuns = runs.data?.runs ?? [];
  const waiting = feedRuns.flatMap((run) => run.waitingInput.map((item) => ({
    run,
    waiting: item as WaitingInput,
  })));
  const interrupted = feedRuns.filter(isInterrupted);
  const active = feedRuns.filter((run) => run.run.active);
  const completed = feedRuns.filter((run) => run.run.status.toLowerCase() === "completed");

  if (waiting.length === 0 && interrupted.length === 0 && active.length === 0 && completed.length === 0) return null;
  return (
    <section aria-label="运行交互" className="run-interaction-feed">
      {active.map((run) => <article className="run-interaction-card run-interaction-card--status" key={run.run.runId}>
        <div className="run-interaction-card__status-copy">
          <div className="run-interaction-card__status-heading">
            <span className="run-interaction-card__live"><i aria-hidden="true" />运行中</span>
            {reportingRunStepSummary(run) ? <small>{reportingRunStepSummary(run)}</small> : null}
          </div>
          <strong>{reportingWorkflowLabel(run.run.workflowId)}运行中</strong>
          <span className="run-interaction-card__stage">{reportingRunStage(run)}</span>
          <span>{run.run.workflowId} · {run.run.runId}</span>
        </div>
        <a aria-label="查看运行态" href={`/projects/${encodeURIComponent(projectId)}/runtime`}>详情</a>
      </article>)}
      {waiting.map(({ run, waiting: item }) => (
        <WaitingInteraction api={api} key={`${run.run.runId}:${waitingInputId(item) ?? "input"}`} run={run} waiting={item} />
      ))}
      {completed.slice(0, 1).map((run) => <RunResult api={api} run={run} projectId={projectId} key={run.run.runId} />)}
      {completed.length > 1 ? <details>
        <summary>更早完成的运行（{completed.length - 1}）</summary>
        {completed.slice(1).map((run) => <RunResult api={api} run={run} projectId={projectId} key={run.run.runId} />)}
      </details> : null}
      {interrupted.map((run) => <article aria-live="assertive" className="run-interaction-card run-interaction-card--resume" key={run.run.runId}>
        <div className="run-interaction-card__failure">
          <strong>{reportingWorkflowLabel(run.run.workflowId)}失败</strong>
          <span>{run.run.workflowId} · {run.run.runId}</span>
          {stateString(run, "error_action_id")
            ? <span>失败阶段 · {reportingActionLabel(stateString(run, "error_action_id")!)}</span>
            : null}
          {failureMessage(run) ? <p role="alert">{failureMessage(run)}</p> : null}
        </div>
        <div className="run-interaction-card__actions">
          <a aria-label="查看运行态" href={`/projects/${encodeURIComponent(projectId)}/runtime`}>详情</a>
          <button disabled={resume.isPending} onClick={() => resume.mutate(run.run.runId)} type="button">恢复原报告</button>
        </div>
      </article>)}
    </section>
  );
}
