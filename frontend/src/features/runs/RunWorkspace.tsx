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

interface SchemaFieldsProps {
  readonly onChange: (name: string, value: boolean | string) => void;
  readonly schema: JsonSchema;
  readonly values: FormValues;
}

function SchemaFields({ onChange, schema, values }: SchemaFieldsProps) {
  return Object.entries(schema.properties ?? {}).map(([name, property]) => {
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
  const waitingId = waitingInputId(waiting);
  const waitingSchemaRaw = waiting?.schema;
  const waitingSchema = schemaValue(waitingSchemaRaw);
  const hasWaitingControls = supportsSchemaControls(waitingSchema);
  const inputSchema = schemaValue(schema.data?.schema);
  const hasInputControls = supportsSchemaControls(inputSchema);
  const inputFormValues = { ...initialFormValues(inputSchema), ...inputValues };
  const waitingFormKey = waitingId ?? (waitingSchemaRaw === undefined ? "" : JSON.stringify(waitingSchemaRaw) ?? "");
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
        <label>工作流<select onChange={(event) => {
          setWorkflowId(event.target.value);
          setInputValues({});
          setInputText("{}");
          setError(null);
        }} value={selectedWorkflowId}>
          {runnable.map((workflow) => <option key={workflow.id}>{workflow.id}</option>)}
        </select></label>
        {hasInputControls ? (
          <fieldset>
            <legend>运行输入</legend>
            <SchemaFields
              onChange={(name, value) => setInputValues((current) => ({ ...current, [name]: value }))}
              schema={inputSchema}
              values={inputFormValues}
            />
          </fieldset>
        ) : (
          <label>运行输入 JSON<textarea
            aria-label="运行输入 JSON"
            onChange={(event) => setInputText(event.target.value)}
            value={inputText}
          /></label>
        )}
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
        <p>{totals.estimated_cost === undefined || totals.estimated_cost === null
          ? "成本未知"
          : String(totals.estimated_cost)}</p>
        {run.data.waitingInput.length > 0 ? <form onSubmit={(event) => { event.preventDefault(); provideInput.mutate(); }}>
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
          <button disabled={provideInput.isPending} type="submit">提交运行输入</button>
        </form> : null}
      </section> : null}
    </section>
  );
}
