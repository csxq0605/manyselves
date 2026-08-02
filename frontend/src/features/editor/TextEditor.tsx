import Editor, { loader, type OnMount } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import { useEffect, useRef } from "react";
import cssWorker from "./workers/css.worker?worker";
import editorWorker from "./workers/editor.worker?worker";
import htmlWorker from "./workers/html.worker?worker";
import jsonWorker from "./workers/json.worker?worker";
import typescriptWorker from "./workers/ts.worker?worker";

import type { EditorTab } from "./editor-store";

export interface TextEditorProps {
  readonly onChange: (content: string) => void;
  readonly onSave: () => void;
  readonly onViewStateChange?: (
    cursor: { readonly column: number; readonly lineNumber: number } | null,
    viewState: unknown,
  ) => void;
  readonly tab: EditorTab;
}

const languageByExtension: Readonly<Record<string, string>> = {
  csv: "plaintext",
  json: "json",
  md: "markdown",
  py: "python",
  yaml: "yaml",
  yml: "yaml",
};

function languageFor(path: string): string {
  const extension = path.split(".").at(-1)?.toLowerCase() ?? "";
  return languageByExtension[extension] ?? "plaintext";
}

globalThis.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    if (label === "json") return new jsonWorker();
    if (label === "css" || label === "scss" || label === "less") return new cssWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new htmlWorker();
    if (label === "typescript" || label === "javascript") return new typescriptWorker();
    return new editorWorker();
  },
};
loader.config({ monaco });

export function TextEditor({ onChange, onSave, onViewStateChange, tab }: TextEditorProps) {
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const viewStateCallback = useRef(onViewStateChange);

  useEffect(() => {
    viewStateCallback.current = onViewStateChange;
  }, [onViewStateChange]);

  useEffect(() => () => {
    const editor = editorRef.current;
    viewStateCallback.current?.(editor?.getPosition() ?? null, editor?.saveViewState() ?? null);
  }, [tab.path]);

  const mount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, onSave);
    if (tab.viewState) {
      editor.restoreViewState(tab.viewState as monaco.editor.ICodeEditorViewState);
    }
    if (tab.cursor) editor.setPosition(tab.cursor);
  };

  return (
    <div aria-label="代码编辑器" className="text-editor">
      <Editor
        height="52vh"
        language={languageFor(tab.path)}
        onChange={(value) => onChange(value ?? "")}
        onMount={mount}
        options={{
          automaticLayout: true,
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
          wordWrap: "on",
        }}
        path={`${tab.projectId}:${tab.path}`}
        theme="vs"
        value={tab.draftContent}
      />
    </div>
  );
}
