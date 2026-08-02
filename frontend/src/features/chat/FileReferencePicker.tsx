import { useMemo, useState } from "react";

import type { components } from "../../api/generated/schema";
import type { SelectionInput } from "../editor/selection-context";

export type FileContext = components["schemas"]["FileContextRequest"];

export interface FileReferencePickerProps {
  readonly availableFiles?: readonly string[] | undefined;
  readonly currentEditorPath?: string | null | undefined;
  readonly currentSelection?: SelectionInput | null | undefined;
  readonly onChange: (contexts: readonly FileContext[]) => void;
  readonly sessionId: string;
}

function contextKey(context: FileContext): string {
  return `${context.type}:${context.file}:${context.startLine ?? ""}:${context.endLine ?? ""}`;
}

export function FileReferencePicker({
  availableFiles = [],
  currentEditorPath,
  currentSelection,
  onChange,
  sessionId,
}: FileReferencePickerProps) {
  const [serverFile, setServerFile] = useState(availableFiles[0] ?? "");
  const [contexts, setContexts] = useState<FileContext[]>([]);
  const selectedServerFile = availableFiles.includes(serverFile)
    ? serverFile
    : availableFiles[0] ?? "";

  function update(next: FileContext[]) {
    const unique = [...new Map(next.map((context) => [contextKey(context), context])).values()];
    setContexts(unique);
    onChange(unique);
  }

  function toggle(context: FileContext, checked: boolean) {
    const key = contextKey(context);
    update(checked ? [...contexts, context] : contexts.filter((item) => contextKey(item) !== key));
  }

  const editorContext = useMemo<FileContext | null>(() => currentEditorPath
    ? { file: currentEditorPath, type: "file" }
    : null, [currentEditorPath]);
  const selectionContext = useMemo<FileContext | null>(() => currentSelection ? {
    endLine: currentSelection.endLine,
    file: currentSelection.path,
    startLine: currentSelection.startLine,
    type: "selection",
  } : null, [currentSelection]);

  return (
    <fieldset className="file-reference-picker" data-session-id={sessionId}>
      <legend>引用服务器文件</legend>
      <label>
        <span>服务器文件</span>
        <select aria-label="服务器文件" onChange={(event) => setServerFile(event.target.value)} value={selectedServerFile}>
          {availableFiles.length === 0 ? <option value="">暂无可引用文件</option> : null}
          {availableFiles.map((path) => <option key={path} value={path}>{path}</option>)}
        </select>
      </label>
      <button
        disabled={!selectedServerFile}
        onClick={() => selectedServerFile && update([...contexts, { file: selectedServerFile, type: "file" }])}
        type="button"
      >添加服务器文件</button>
      <label>
        <input
          checked={editorContext ? contexts.some((item) => contextKey(item) === contextKey(editorContext)) : false}
          disabled={!editorContext}
          onChange={(event) => editorContext && toggle(editorContext, event.target.checked)}
          type="checkbox"
        />
        引用当前编辑文件
      </label>
      <label>
        <input
          checked={selectionContext ? contexts.some((item) => contextKey(item) === contextKey(selectionContext)) : false}
          disabled={!selectionContext}
          onChange={(event) => selectionContext && toggle(selectionContext, event.target.checked)}
          type="checkbox"
        />
        引用当前选区
      </label>
      {contexts.length > 0 ? (
        <ul aria-label="已选引用">
          {contexts.map((context) => <li key={contextKey(context)}>{context.file} · {context.type}</li>)}
        </ul>
      ) : null}
    </fieldset>
  );
}
