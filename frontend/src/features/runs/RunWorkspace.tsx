import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { createUuid } from "../../app/uuid";
import type { WorkflowApi } from "./workflow-api";
import "./run-workspace.css";

export interface RunWorkspaceProps {
  readonly api: WorkflowApi;
}

type JsonSchema = {
  readonly $ref?: string;
  readonly anyOf?: readonly JsonSchema[];
  readonly description?: string;
  readonly default?: unknown;
  readonly enum?: readonly unknown[];
  readonly items?: JsonSchema;
  readonly maximum?: number;
  readonly maxItems?: number;
  readonly maxLength?: number;
  readonly minimum?: number;
  readonly minItems?: number;
  readonly minLength?: number;
  readonly nullable?: boolean;
  readonly oneOf?: readonly JsonSchema[];
  readonly properties?: Record<string, JsonSchema>;
  readonly required?: readonly string[];
  readonly title?: string;
  readonly type?: string | readonly string[];
};

type SchemaControlKind = "array" | "boolean" | "enum" | "integer" | "json" | "number" | "string";
type FormValues = Record<string, boolean | string>;
type ContinuationState = {
  readonly key: string;
  readonly text: string;
  readonly values: FormValues;
};
type SchemaControlInfo = {
  readonly kind: SchemaControlKind;
  readonly nullable: boolean;
  readonly schema: JsonSchema;
};

function objectValue(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function parseJson(value: string): unknown {
  return JSON.parse(value) as unknown;
}

function schemaValue(value: unknown): JsonSchema {
  return objectValue(value) as JsonSchema;
}

function schemaTypes(schema: JsonSchema): readonly string[] {
  if (Array.isArray(schema.type)) return schema.type;
  return typeof schema.type === "string" ? [schema.type] : [];
}

function hasSchemaDefault(schema: JsonSchema): boolean {
  return Object.prototype.hasOwnProperty.call(schema, "default");
}

function schemaVariants(schema: JsonSchema): { nullable: boolean; schema: JsonSchema } | null {
  const union = schema.anyOf ?? schema.oneOf;
  if (union === undefined) {
    return { nullable: schema.nullable === true || schemaTypes(schema).includes("null"), schema };
  }
  const nonNull = union.filter((variant) => !schemaTypes(variant).includes("null"));
  const nullable = schema.nullable === true || nonNull.length !== union.length;
  if (nonNull.length === 0) return null;
  if (nonNull.length === 1) return { nullable, schema: nonNull[0]! };
  return { nullable, schema };
}

function schemaControlInfo(schema: JsonSchema): SchemaControlInfo | null {
  const variant = schemaVariants(schema);
  if (variant === null) return null;
  const effective = variant.schema;
  const enumValues = effective.enum ?? schema.enum;
  if (enumValues !== undefined && enumValues.length > 0) {
    return {
      kind: "enum",
      nullable: variant.nullable || enumValues.includes(null),
      schema: effective,
    };
  }
  if ((effective.anyOf ?? effective.oneOf) !== undefined) {
    return { kind: "json", nullable: variant.nullable, schema: effective };
  }
  const types = schemaTypes(effective);
  if (types.includes("boolean")) return { kind: "boolean", nullable: variant.nullable, schema: effective };
  if (types.includes("integer")) return { kind: "integer", nullable: variant.nullable, schema: effective };
  if (types.includes("number")) return { kind: "number", nullable: variant.nullable, schema: effective };
  if (types.includes("string")) return { kind: "string", nullable: variant.nullable, schema: effective };
  if (types.includes("array")) {
    const itemInfo = effective.items === undefined ? null : schemaControlInfo(effective.items);
    const primitive = itemInfo !== null && ["boolean", "enum", "integer", "number", "string"].includes(itemInfo.kind);
    return { kind: primitive ? "array" : "json", nullable: variant.nullable, schema: effective };
  }
  if (types.includes("object") || effective.$ref !== undefined || effective.properties !== undefined) {
    return { kind: "json", nullable: variant.nullable, schema: effective };
  }
  return null;
}

function supportsSchemaControls(schema: JsonSchema): boolean {
  const properties = schema.properties;
  if (properties === undefined || Object.keys(properties).length === 0) return false;
  if (schema.type !== undefined && !schemaTypes(schema).includes("object")) return false;
  return Object.values(properties).every((property) => schemaControlInfo(property) !== null);
}

function encodeEnumValue(value: unknown): string {
  const encoded = JSON.stringify(value);
  return encoded === undefined ? String(value) : encoded;
}

function encodeJsonValue(value: unknown): string {
  const encoded = JSON.stringify(value, null, 2);
  return encoded === undefined ? "" : encoded;
}

function encodeArrayValue(value: unknown): string {
  if (!Array.isArray(value)) return encodeJsonValue(value);
  return value.map((item) => typeof item === "string" ? item : encodeJsonValue(item)).join("\n");
}

function fieldDefault(schema: JsonSchema, info: SchemaControlInfo): unknown {
  if (hasSchemaDefault(schema)) return schema.default;
  if (hasSchemaDefault(info.schema)) return info.schema.default;
  return undefined;
}

function initialFieldValue(schema: JsonSchema): boolean | string {
  const info = schemaControlInfo(schema);
  if (info === null) return "";
  const declaredDefault = fieldDefault(schema, info);
  if (declaredDefault !== undefined || hasSchemaDefault(schema) || hasSchemaDefault(info.schema)) {
    if (declaredDefault === null) return "";
    if (info.kind === "boolean") return declaredDefault === true;
    if (info.kind === "array") return encodeArrayValue(declaredDefault);
    if (info.kind === "json") return encodeJsonValue(declaredDefault);
    if (info.kind === "string" || info.kind === "number" || info.kind === "integer") {
      return String(declaredDefault);
    }
    return encodeEnumValue(declaredDefault);
  }
  if (info.kind === "boolean") return false;
  if (info.kind === "enum") {
    const first = (info.schema.enum ?? schema.enum ?? []).find((value) => value !== null);
    return first === undefined ? "" : encodeEnumValue(first);
  }
  return "";
}

function initialFormValues(schema: JsonSchema): FormValues {
  const values: FormValues = {};
  for (const [name, property] of Object.entries(schema.properties ?? {})) {
    values[name] = initialFieldValue(property);
  }
  return values;
}

function parseArrayItem(value: string, schema: JsonSchema): unknown {
  const info = schemaControlInfo(schema);
  if (info?.kind === "boolean") return value.trim().toLowerCase() === "true";
  if (info?.kind === "integer" || info?.kind === "number") return Number(value.trim());
  if (info?.kind === "enum") {
    try {
      return JSON.parse(value.trim());
    } catch {
      return value.trim();
    }
  }
  return value.trim();
}

function parseArrayValue(value: string, schema: JsonSchema): unknown[] {
  const trimmed = value.trim();
  if (!trimmed) return [];
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (Array.isArray(parsed)) return parsed;
  } catch {
    // Multiline and comma-separated primitive values are also accepted.
  }
  return trimmed.split(/[\n,]/).map((item) => parseArrayItem(item, schema));
}

function formValuesToObject(schema: JsonSchema, values: FormValues): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [name, property] of Object.entries(schema.properties ?? {})) {
    const info = schemaControlInfo(property);
    const value = values[name] ?? initialFieldValue(property);
    if (info === null || value === undefined) continue;
    const required = schema.required?.includes(name) ?? false;
    if (info.kind === "boolean") {
      result[name] = info.nullable && value === "" ? null : value === true || value === "true";
    } else if ((info.kind === "number" || info.kind === "integer") && value !== "") {
      result[name] = Number(value);
    } else if ((info.kind === "number" || info.kind === "integer") && info.nullable) {
      result[name] = null;
    } else if (info.kind === "enum" && value !== "") {
      try {
        result[name] = JSON.parse(String(value));
      } catch {
        result[name] = value;
      }
    } else if (info.kind === "enum" && info.nullable) {
      result[name] = null;
    } else if (info.kind === "array" && value !== "") {
      result[name] = parseArrayValue(String(value), info.schema.items ?? {});
    } else if (info.kind === "array" && info.nullable) {
      result[name] = null;
    } else if (info.kind === "array" && required) {
      result[name] = [];
    } else if (info.kind === "json" && value !== "") {
      try {
        result[name] = JSON.parse(String(value));
      } catch {
        throw new Error(`${name} 必须是有效 JSON。`);
      }
    } else if (info.kind === "json" && info.nullable) {
      result[name] = null;
    } else if (info.kind === "string" && value === "" && info.nullable) {
      result[name] = null;
    } else if (info.kind === "string" || value !== "") {
      result[name] = value;
    }
  }
  return result;
}

function waitingInputId(waiting: Record<string, unknown> | undefined): string | undefined {
  const inputId = waiting?.input_id ?? waiting?.inputId ?? waiting?.decision_id ?? waiting?.decisionId;
  return typeof inputId === "string" ? inputId : undefined;
}

function waitingContractId(waiting: Record<string, unknown> | undefined): string | undefined {
  const contractId = waiting?.contract_id ?? waiting?.contractId
    ?? waiting?.input_contract ?? waiting?.inputContract;
  return typeof contractId === "string" ? contractId : undefined;
}

function waitingText(
  waiting: Record<string, unknown> | undefined,
  name: "description" | "title",
): string | undefined {
  const value = waiting?.[name];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function waitingPathText(waiting: Record<string, unknown> | undefined): string | undefined {
  const path = waiting?.path;
  if (!Array.isArray(path) || path.length === 0) return undefined;
  const segments = path.map((segment) => {
    if (typeof segment === "string") return segment;
    const item = objectValue(segment);
    const label = item.action_id ?? item.actionId ?? item.workflow_id ?? item.workflowId
      ?? item.input_id ?? item.inputId ?? item.id ?? item.name ?? item.kind;
    return typeof label === "string" ? label : JSON.stringify(segment);
  });
  return segments.join(" → ");
}

function waitingFormIdentity(
  waiting: Record<string, unknown> | undefined,
  schema: unknown,
): string {
  return JSON.stringify([
    waitingInputId(waiting) ?? null,
    waitingContractId(waiting) ?? null,
    waiting?.path ?? null,
    schema ?? null,
  ]);
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined) return "";
  const encoded = JSON.stringify(value);
  return encoded === undefined ? String(value) : encoded;
}

function errorFromState(state: Record<string, unknown>): string | undefined {
  for (const key of ["error", "error_message", "message"]) {
    const value = state[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return undefined;
}

function statusLabel(status: string): string {
  switch (status.toLowerCase()) {
    case "running":
    case "in_progress":
    case "active":
      return "运行中";
    case "waiting":
    case "waiting_input":
    case "waiting_user":
      return "等待输入";
    case "completed":
    case "success":
      return "已完成";
    case "failed":
    case "error":
      return "失败";
    default:
      return status || "未知";
  }
}

function statusClass(status: string): string {
  switch (status.toLowerCase()) {
    case "running":
    case "in_progress":
    case "active":
      return "running";
    case "waiting":
    case "waiting_input":
    case "waiting_user":
      return "waiting";
    case "completed":
    case "success":
      return "completed";
    case "failed":
    case "error":
      return "failed";
    default:
      return "unknown";
  }
}

function isRunningStatus(status: string): boolean {
  return statusClass(status) === "running";
}

function schemaFieldNames(schema: JsonSchema, required: boolean): string[] {
  const requiredNames = new Set(schema.required ?? []);
  return Object.keys(schema.properties ?? {}).filter((name) => requiredNames.has(name) === required);
}

interface SchemaFieldsProps {
  readonly onChange: (name: string, value: boolean | string) => void;
  readonly schema: JsonSchema;
  readonly values: FormValues;
  readonly names?: readonly string[];
}

function SchemaFields({ onChange, names, schema, values }: SchemaFieldsProps) {
  const entries = Object.entries(schema.properties ?? {}).filter(([name]) => names === undefined || names.includes(name));
  return entries.map(([name, property]) => {
    const info = schemaControlInfo(property);
    if (info === null) return null;
    const label = property.title ?? info.schema.title ?? name;
    const required = schema.required?.includes(name) ?? false;
    const value = values[name] ?? initialFieldValue(property);
    const labelText = required ? `${label} *` : label;
    const description = property.description ?? info.schema.description;
    const enumValues = info.schema.enum ?? property.enum ?? [];
    const requiredAttribute = required && !info.nullable && info.kind !== "boolean";

    if (info.kind === "enum") {
      return (
        <label key={name}>
          <span>{labelText}</span>
          <select
            aria-label={label}
            onChange={(event) => onChange(name, event.currentTarget.value)}
            required={requiredAttribute}
            value={typeof value === "string" ? value : ""}
          >
            {info.nullable ? <option value="">未设置</option> : null}
            {enumValues.filter((option) => option !== null).map((option) => {
              const encoded = encodeEnumValue(option);
              return <option key={encoded} value={encoded}>{String(option)}</option>;
            })}
          </select>
          {description ? <small>{description}</small> : null}
        </label>
      );
    }

    if (info.kind === "boolean" && info.nullable) {
      const nullableBooleanValue = value === true ? "true" : value === false ? "false" : "";
      return (
        <label key={name}>
          <span>{labelText}</span>
          <select
            aria-label={label}
            onChange={(event) => onChange(name, event.currentTarget.value)}
            value={nullableBooleanValue}
          >
            <option value="">未设置</option>
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
          {description ? <small>{description}</small> : null}
        </label>
      );
    }

    if (info.kind === "boolean") {
      return (
        <label key={name}>
          <span>{labelText}</span>
          <input
            aria-label={label}
            checked={value === true || value === "true"}
            onChange={(event) => onChange(name, event.currentTarget.checked)}
            type="checkbox"
          />
          {description ? <small>{description}</small> : null}
        </label>
      );
    }

    if (info.kind === "array") {
      return (
        <label key={name}>
          <span>{labelText}</span>
          <textarea
            aria-label={label}
            onChange={(event) => onChange(name, event.currentTarget.value)}
            required={requiredAttribute}
            rows={3}
            value={typeof value === "string" ? value : ""}
          />
          <small>每行或逗号分隔，也支持 JSON 数组。</small>
          {description ? <small>{description}</small> : null}
        </label>
      );
    }

    if (info.kind === "json") {
      return (
        <label key={name}>
          <span>{labelText}</span>
          <textarea
            aria-label={label}
            onChange={(event) => onChange(name, event.currentTarget.value)}
            required={requiredAttribute}
            rows={4}
            value={typeof value === "string" ? value : ""}
          />
          {description ? <small>{description}</small> : null}
        </label>
      );
    }

    return (
      <label key={name}>
        <span>{labelText}</span>
        <input
          aria-label={label}
          onChange={(event) => onChange(name, event.currentTarget.value)}
          max={info.schema.maximum ?? property.maximum}
          maxLength={info.schema.maxLength ?? property.maxLength}
          min={info.schema.minimum ?? property.minimum}
          minLength={info.schema.minLength ?? property.minLength}
          required={requiredAttribute}
          step={info.kind === "integer" ? "1" : info.kind === "number" ? "any" : undefined}
          type={info.kind === "string" ? "text" : "number"}
          value={typeof value === "string" ? value : ""}
        />
        {description ? <small>{description}</small> : null}
      </label>
    );
  });
}

export function RunWorkspace({ api }: RunWorkspaceProps) {
  const [workflowId, setWorkflowId] = useState("");
  const [runId, setRunId] = useState("");
  const [runLookupId, setRunLookupId] = useState("");
  const [inputText, setInputText] = useState("{}");
  const [inputValues, setInputValues] = useState<FormValues>({});
  const [continuationState, setContinuationState] = useState<ContinuationState>({
    key: "",
    text: "{}",
    values: {},
  });
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
  const waiting = run.data?.waitingInput[0];
  const waitingSchemaRaw = waiting?.schema;
  const waitingSchema = schemaValue(waitingSchemaRaw);
  const waitingTitle = waitingText(waiting, "title") ?? waitingSchema.title;
  const waitingDescription = waitingText(waiting, "description") ?? waitingSchema.description;
  const waitingPath = waitingPathText(waiting);
  const hasWaitingControls = supportsSchemaControls(waitingSchema);
  const inputSchema = schemaValue(schema.data?.schema);
  const hasInputControls = supportsSchemaControls(inputSchema);
  const inputFormValues = { ...initialFormValues(inputSchema), ...inputValues };
  const waitingFormKey = waitingFormIdentity(waiting, waitingSchemaRaw);
  const continuationValues = continuationState.key === waitingFormKey ? continuationState.values : {};
  const continuationText = continuationState.key === waitingFormKey ? continuationState.text : "{}";
  const continuationFormValues = { ...initialFormValues(waitingSchema), ...continuationValues };
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
  const events = useQuery({
    enabled: Boolean(runId && api.events),
    queryFn: () => api.events!(runId),
    queryKey: ["runs", runId, "events"],
    refetchInterval: () => run.data?.run.active ? 1000 : false,
  });
  const refetchOutputs = outputs.refetch;
  const refetchCost = cost.refetch;
  useEffect(() => {
    if (!runId || run.data === undefined || run.data.run.active) return;
    void refetchOutputs();
    void refetchCost();
  }, [refetchCost, refetchOutputs, run.data, runId]);
  const start = useMutation({
    mutationFn: () => api.start({
      input: hasInputControls ? formValuesToObject(inputSchema, inputFormValues) : parseJson(inputText),
      workflowId: selectedWorkflowId,
    }, createUuid()),
    onError: (reason) => setError(reason instanceof Error ? reason.message : "启动失败。"),
    onSuccess: (accepted) => {
      setError(null);
      setRunId(accepted.runId);
    },
  });
  const provideInput = useMutation({
    mutationFn: () => {
      const inputId = waitingInputId(waiting);
      return api.provideInput(
        runId,
        {
          ...(inputId ? { inputId } : {}),
          values: hasWaitingControls
            ? formValuesToObject(waitingSchema, continuationFormValues)
            : parseJson(continuationText),
        },
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
  const resumeRun = useMutation({
    mutationFn: () => api.resume(runId, createUuid()),
    onError: (reason) => setError(reason instanceof Error ? reason.message : "恢复运行失败。"),
    onSuccess: (accepted) => {
      setError(null);
      setRunId(accepted.runId);
      void run.refetch();
      void outputs.refetch();
      void cost.refetch();
    },
  });

  const totals = objectValue(cost.data?.usage.totals);
  const estimatedCost = totals.estimated_cost;
  const pricingStatus = typeof totals.pricing_status === "string"
    ? totals.pricing_status
    : estimatedCost === undefined || estimatedCost === null ? "unknown" : "configured";
  const requiredInputNames = schemaFieldNames(inputSchema, true);
  const optionalInputNames = schemaFieldNames(inputSchema, false);
  const rawRunStatus = run.data?.run.status ?? "";
  const runStatusClass = statusClass(rawRunStatus);
  const canResumeRun = run.data !== undefined
    && !run.data.run.active
    && run.data.waitingInput.length === 0
    && isRunningStatus(rawRunStatus);
  const runStateError = run.data ? errorFromState(objectValue(run.data.state)) : undefined;
  const eventError = events.data?.events.find((event) => typeof event.error === "string" && event.error.length > 0)?.error;
  const failureError = runStateError ?? eventError;
  const visibleEvents = events.data?.events.slice(-100) ?? [];
  const eventLimitApplied = (events.data?.events.length ?? 0) > visibleEvents.length;
  return (
    <section className="run-workspace" aria-label="通用工作流">
      <header className="run-workspace__header">
        <div>
          <p className="run-workspace__eyebrow">CAPABILITY RUNTIME / WORKFLOWS</p>
          <h1>通用工作流</h1>
          <span className="run-workspace__subtitle">用声明式定义启动、观察并继续任意工作流。</span>
        </div>
        {run.data ? (
          <span className={`run-workspace__status-pill run-workspace__status-pill--${runStatusClass}`}>
            <i aria-hidden="true" />{statusLabel(rawRunStatus)}
          </span>
        ) : <span className="run-workspace__status-pill run-workspace__status-pill--idle">待启动</span>}
      </header>
      {capabilities.isPending || workflows.isPending ? <p className="run-workspace__notice" role="status">正在加载能力定义…</p> : null}
      {capabilities.isError || workflows.isError ? <p className="run-workspace__notice run-workspace__notice--error" role="alert">能力定义加载失败。</p> : null}
      <section className="run-workspace__capability-strip" aria-label="能力概览">
        <div className="run-workspace__section-heading">
          <div><p className="run-workspace__eyebrow">AVAILABLE DEFINITIONS</p><h2>能力概览</h2></div>
          <span>{capabilities.data?.capabilities.length ?? 0} 项能力 · {runnable.length} 个可运行工作流</span>
        </div>
        <div className="run-workspace__capabilities">
          {capabilities.data?.capabilities.map((capability) => (
            <article key={capability.id}>
              <div className="run-workspace__capability-mark" aria-hidden="true">◎</div>
              <div><strong>{capability.id}</strong><span>{capability.description}</span></div>
              <small>v{capability.version}</small>
            </article>
          ))}
          {capabilities.data && capabilities.data.capabilities.length === 0 ? <p className="run-workspace__empty">暂未注册能力。</p> : null}
        </div>
      </section>
      <div className="run-workspace__layout">
        <aside className="run-workspace__setup" aria-label="工作流配置">
          <div className="run-workspace__section-heading run-workspace__section-heading--compact">
            <div><p className="run-workspace__eyebrow">CONFIGURE</p><h2>启动配置</h2></div>
            <span>定义驱动</span>
          </div>
          <form
            className="run-workspace__card run-workspace__start-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (selectedWorkflowId && !start.isPending) start.mutate();
            }}
          >
            <label>工作流<select aria-label="工作流" onChange={(event) => {
              setWorkflowId(event.target.value);
              setInputValues({});
              setInputText("{}");
              setError(null);
            }} value={selectedWorkflowId}>
              {runnable.map((workflow) => <option key={workflow.id}>{workflow.id}</option>)}
            </select></label>
            {hasInputControls ? (
              <div className="run-workspace__schema-form" aria-label="运行输入">
                <div className="run-workspace__form-heading"><h3>运行输入</h3><span>按 Schema 填写</span></div>
                {requiredInputNames.length > 0 ? <fieldset>
                  <legend>必填参数</legend>
                  <SchemaFields
                    names={requiredInputNames}
                    onChange={(name, value) => setInputValues((current) => ({ ...current, [name]: value }))}
                    schema={inputSchema}
                    values={inputFormValues}
                  />
                </fieldset> : null}
                {optionalInputNames.length > 0 ? <details className="run-workspace__optional-fields" open={requiredInputNames.length === 0}>
                  <summary>可选参数 <span>{optionalInputNames.length}</span></summary>
                  <fieldset>
                    <legend className="run-workspace__visually-hidden">可选参数</legend>
                    <SchemaFields
                      names={optionalInputNames}
                      onChange={(name, value) => setInputValues((current) => ({ ...current, [name]: value }))}
                      schema={inputSchema}
                      values={inputFormValues}
                    />
                  </fieldset>
                </details> : null}
              </div>
            ) : (
              <label>运行输入 JSON<textarea
                aria-label="运行输入 JSON"
                onChange={(event) => setInputText(event.target.value)}
                value={inputText}
              /></label>
            )}
            {schema.data ? <details className="run-workspace__developer-details"><summary>查看输入 Schema</summary><pre>{JSON.stringify(schema.data.schema, null, 2)}</pre></details> : null}
            <button className="run-workspace__primary-action" disabled={!selectedWorkflowId || start.isPending} type="submit">
              <span>{start.isPending ? "正在启动…" : "启动工作流"}</span><span aria-hidden="true">↗</span>
            </button>
          </form>
          {error ? <p className="run-workspace__notice run-workspace__notice--error" role="alert">{error}</p> : null}
          {schema.isError ? <p className="run-workspace__notice run-workspace__notice--error" role="alert">输入 Schema 加载失败。</p> : null}
        </aside>
        <section className="run-workspace__console" aria-label="运行控制台">
          {!run.data ? <section className="run-workspace__empty-console">
            <div className="run-workspace__empty-icon" aria-hidden="true">↗</div>
            <h2>运行控制台</h2>
            <p>启动一个工作流后，这里会显示状态、人工输入、输出、事件和成本。</p>
            <form
              className="run-workspace__reopen-form"
              onSubmit={(event) => {
                event.preventDefault();
                const requestedRunId = runLookupId.trim();
                if (!requestedRunId) return;
                setError(null);
                setRunId(requestedRunId);
              }}
            >
              <label>打开已有运行<input
                aria-label="Run ID"
                onChange={(event) => setRunLookupId(event.currentTarget.value)}
                placeholder="粘贴 Run ID"
                required
                value={runLookupId}
              /></label>
              <button type="submit">打开运行</button>
            </form>
          </section> : <>
            <section className={`run-workspace__run run-workspace__run--${runStatusClass}`} aria-label="运行结果">
              <div className="run-workspace__run-heading">
                <div><p className="run-workspace__eyebrow">RUN CONTROL</p><h2>{run.data.run.runId}</h2></div>
                <span className="run-workspace__run-status"><i aria-hidden="true" />{statusLabel(rawRunStatus)}</span>
              </div>
              <dl className="run-workspace__run-meta">
                <div><dt>Capability</dt><dd>{run.data.run.capabilityId}</dd></div>
                <div><dt>Workflow</dt><dd>{run.data.run.workflowId}</dd></div>
                <div><dt>状态</dt><dd>{rawRunStatus}</dd></div>
                {run.data.run.taskId ? <div><dt>Task</dt><dd>{run.data.run.taskId}</dd></div> : null}
              </dl>
              {canResumeRun ? <button
                className="run-workspace__primary-action"
                disabled={resumeRun.isPending}
                onClick={() => resumeRun.mutate()}
                type="button"
              >
                {resumeRun.isPending ? "正在恢复…" : "恢复运行"}
              </button> : null}
            </section>
            {run.isError ? <section className="run-workspace__panel run-workspace__panel--error" aria-label="运行加载错误" role="alert">
              <h3>无法读取运行状态</h3><p>{run.error instanceof Error ? run.error.message : "运行状态加载失败。"}</p>
            </section> : null}
            {runStatusClass === "failed" ? <section className="run-workspace__panel run-workspace__panel--error" aria-label="运行失败" role="alert">
              <div><span className="run-workspace__panel-kicker">EXECUTION ERROR</span><h3>运行失败</h3></div>
              <p>{failureError ?? "运行以失败状态结束，未提供更多错误信息。"}</p>
            </section> : null}
            {run.data.waitingInput.length > 0 ? <form className="run-workspace__waiting run-workspace__panel" onSubmit={(event) => { event.preventDefault(); provideInput.mutate(); }}>
              <div className="run-workspace__waiting-heading"><span className="run-workspace__panel-kicker">ACTION REQUIRED</span><span className="run-workspace__status-label">等待输入</span></div>
              {waitingTitle ? <h3>{waitingTitle}</h3> : <h3>需要继续输入</h3>}
              {waitingDescription ? <p>{waitingDescription}</p> : null}
              {waitingPath ? <p className="run-workspace__path">{`路径：${waitingPath}`}</p> : null}
              {hasWaitingControls ? (
                <fieldset>
                  <legend>继续输入</legend>
                  <SchemaFields
                    onChange={(name, value) => setContinuationState({
                      key: waitingFormKey,
                      text: "{}",
                      values: { ...continuationValues, [name]: value },
                    })}
                    schema={waitingSchema}
                    values={continuationFormValues}
                  />
                </fieldset>
              ) : (
                <label>继续输入 JSON<textarea
                  aria-label="继续输入 JSON"
                  onChange={(event) => setContinuationState({
                    key: waitingFormKey,
                    text: event.target.value,
                    values: {},
                  })}
                  value={continuationText}
                /></label>
              )}
              <button className="run-workspace__primary-action" disabled={provideInput.isPending} type="submit">
                {provideInput.isPending ? "正在提交…" : "提交运行输入"}
              </button>
            </form> : null}
            <div className="run-workspace__telemetry-grid">
              <section className="run-workspace__panel" aria-label="Outputs">
                <div className="run-workspace__panel-heading"><div><span className="run-workspace__panel-kicker">ARTIFACTS</span><h3>Outputs</h3></div><span>{outputs.data?.outputs.length ?? 0}</span></div>
                {outputs.isPending && outputs.data === undefined ? <p className="run-workspace__muted" role="status">正在加载输出…</p> : null}
                {outputs.isError ? <p className="run-workspace__inline-error" role="alert">输出加载失败。</p> : null}
                {outputs.data && outputs.data.outputs.length === 0 ? <p className="run-workspace__muted">暂无输出。</p> : null}
                {outputs.data && outputs.data.outputs.length > 0 ? <ul className="run-workspace__data-list">{outputs.data.outputs.map((output) => (
                  <li key={output.id}><span>{output.path ?? displayValue(output.value)}</span>{typeof output.size === "number" ? <small>{output.size} B</small> : null}</li>
                ))}</ul> : null}
              </section>
              {api.events ? <section className="run-workspace__panel" aria-label="Events">
                <div className="run-workspace__panel-heading"><div><span className="run-workspace__panel-kicker">TRACE</span><h3>Events</h3></div><span>{events.data?.events.length ?? 0}</span></div>
                {events.isPending && events.data === undefined ? <p className="run-workspace__muted" role="status">正在加载事件…</p> : null}
                {events.isError ? <p className="run-workspace__inline-error" role="alert">事件加载失败。</p> : null}
                {events.data && events.data.events.length === 0 ? <p className="run-workspace__muted">暂无事件。</p> : null}
                {eventLimitApplied ? <p className="run-workspace__muted">最近 100 条</p> : null}
                {visibleEvents.length > 0 ? <ol className="run-workspace__event-list">{visibleEvents.map((event, index) => (
                  <li key={`${event.kind}-${index}`} className={event.error ? "run-workspace__event--error" : undefined}>
                    <span>{event.kind}</span>{event.actionId ? <small>{event.actionId}</small> : null}{event.error ? <p>{event.error}</p> : null}
                  </li>
                ))}</ol> : null}
              </section> : null}
              <section className="run-workspace__panel" aria-label="Cost">
                <div className="run-workspace__panel-heading"><div><span className="run-workspace__panel-kicker">METERING</span><h3>Cost</h3></div><span>{pricingStatus}</span></div>
                {cost.isPending && cost.data === undefined ? <p className="run-workspace__muted" role="status">正在加载成本…</p> : null}
                {cost.isError ? <p className="run-workspace__inline-error" role="alert">成本加载失败。</p> : null}
                {cost.data !== undefined ? <div className="run-workspace__cost-summary">
                  <strong>{String(totals.total_tokens ?? 0)} tokens</strong>
                  <span>定价状态：{pricingStatus}</span>
                  <span>{estimatedCost === undefined || estimatedCost === null ? "成本未知" : String(estimatedCost)}</span>
                </div> : null}
              </section>
            </div>
            <details className="run-workspace__developer-details run-workspace__developer-details--run">
              <summary>开发者细节</summary>
              <dl className="run-workspace__run-meta"><div><dt>Run ID</dt><dd>{run.data.run.runId}</dd></div><div><dt>State</dt><dd><pre>{JSON.stringify(run.data.state, null, 2)}</pre></dd></div></dl>
            </details>
          </>}
        </section>
      </div>
    </section>
  );
}
