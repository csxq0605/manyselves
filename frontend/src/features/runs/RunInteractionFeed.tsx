import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { createUuid } from "../../app/uuid";
import { useRunStore } from "../../store/run-store";
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

export interface RunInteractionFeedProps {
  readonly api: WorkflowApi & WorkflowRunFeedApi;
  readonly projectId: string;
}

export function RunInteractionFeed({ api, projectId }: RunInteractionFeedProps) {
  const client = useQueryClient();
  const setCurrentRun = useRunStore((state) => state.setCurrentRun);
  const runs = useQuery({
    queryFn: () => api.listRuns(),
    queryKey: ["runs", "interaction-feed"],
    refetchInterval: 5_000,
  });
  const resume = useMutation({
    mutationFn: (runId: string) => api.resume(runId, createUuid()),
    onSuccess: (accepted) => {
      setCurrentRun(projectId, accepted.runId);
      void client.invalidateQueries({ queryKey: ["runs", "interaction-feed"] });
    },
  });
  const waiting = runs.data?.runs.flatMap((run) => run.waitingInput.map((item) => ({
    run,
    waiting: item as WaitingInput,
  }))) ?? [];
  const interrupted = runs.data?.runs.filter(isInterrupted) ?? [];
  const active = runs.data?.runs.filter((run) => run.run.active) ?? [];
  const workflowHref = `/projects/${encodeURIComponent(projectId)}/workflows`;

  if (waiting.length === 0 && interrupted.length === 0 && active.length === 0) return null;
  return (
    <section aria-label="运行交互" className="run-interaction-feed">
      {active.map((run) => <article className="run-interaction-card run-interaction-card--status" key={run.run.runId}>
        <div>
          <strong>运行中</strong>
          <span>{run.run.workflowId} · {run.run.runId}</span>
        </div>
        <a href={workflowHref} onClick={() => setCurrentRun(projectId, run.run.runId)}>查看运行</a>
      </article>)}
      {waiting.map(({ run, waiting: item }) => (
        <WaitingInteraction api={api} key={`${run.run.runId}:${waitingInputId(item) ?? "input"}`} run={run} waiting={item} />
      ))}
      {interrupted.map((run) => <article className="run-interaction-card run-interaction-card--resume" key={run.run.runId}>
        <div>
          <strong>运行已中断</strong>
          <span>{run.run.workflowId} · {run.run.runId}</span>
        </div>
        <div className="run-interaction-card__actions">
          <a href={workflowHref} onClick={() => setCurrentRun(projectId, run.run.runId)}>查看运行</a>
          <button disabled={resume.isPending} onClick={() => resume.mutate(run.run.runId)} type="button">恢复原运行</button>
        </div>
      </article>)}
    </section>
  );
}
