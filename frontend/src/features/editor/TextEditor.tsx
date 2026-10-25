import Editor, { loader, type OnMount } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import { useEffect, useRef } from "react";

import { useColorScheme } from "../../design/use-color-scheme";
import cssWorker from "./workers/css.worker?worker";
import editorWorker from "./workers/editor.worker?worker";
import htmlWorker from "./workers/html.worker?worker";
import jsonWorker from "./workers/json.worker?worker";
import typescriptWorker from "./workers/ts.worker?worker";

import type { EditorTab } from "./editor-store";

export interface TextEditorProps {
  readonly onChange: (content: string) => void;
  readonly onSave: () => void;
  readonly onSelectionChange?: ((selection: TextEditorSelection | null) => void) | undefined;
  readonly onViewStateChange?: (
    cursor: { readonly column: number; readonly lineNumber: number } | null,
    viewState: unknown,
  ) => void;
  readonly tab: EditorTab;
}

export interface TextEditorSelection {
  readonly endLine: number;
  readonly startLine: number;
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

// Editor themes mirror the design tokens (Monaco needs literal colours, not CSS variables).
monaco.editor.defineTheme("manyselves-light", {
  base: "vs",
  colors: {
    "editor.background": "#fcfbf8",
    "editor.foreground": "#1b2129",
    "editor.lineHighlightBackground": "#f4f2ec",
    "editor.selectionBackground": "#f5e3d8",
    "editorCursor.foreground": "#a8441b",
    "editorLineNumber.activeForeground": "#1b2129",
    "editorLineNumber.foreground": "#767b82",
    "editorWidget.background": "#fcfbf8",
    "editorWidget.border": "#ddd8cc",
  },
  inherit: true,
  rules: [],
});
monaco.editor.defineTheme("manyselves-dark", {
  base: "vs-dark",
  colors: {
    "editor.background": "#1a1f26",
    "editor.foreground": "#ebe7df",
    "editor.lineHighlightBackground": "#13171c",
    "editor.selectionBackground": "#3a2419",
    "editorCursor.foreground": "#e3804f",
    "editorLineNumber.activeForeground": "#ebe7df",
    "editorLineNumber.foreground": "#868d96",
    "editorWidget.background": "#1a1f26",
    "editorWidget.border": "#2b323b",
  },
  inherit: true,
  rules: [],
});

export function TextEditor({ onChange, onSave, onSelectionChange, onViewStateChange, tab }: TextEditorProps) {
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const colorScheme = useColorScheme();
  const selectionCallback = useRef(onSelectionChange);
  const viewStateCallback = useRef(onViewStateChange);

  useEffect(() => {
    viewStateCallback.current = onViewStateChange;
  }, [onViewStateChange]);

  useEffect(() => {
    selectionCallback.current = onSelectionChange;
  }, [onSelectionChange]);

  useEffect(() => () => {
    const editor = editorRef.current;
    viewStateCallback.current?.(editor?.getPosition() ?? null, editor?.saveViewState() ?? null);
  }, [tab.path]);

  const mount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, onSave);
    editor.onDidChangeCursorSelection(({ selection }) => {
      selectionCallback.current?.({
        endLine: selection.endLineNumber,
        startLine: selection.startLineNumber,
      });
    });
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
          fontFamily: "\"IBM Plex Mono\", ui-monospace, \"SFMono-Regular\", monospace",
          fontSize: 13,
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
          wordWrap: "on",
        }}
        path={`${tab.projectId}:${tab.path}`}
        theme={colorScheme === "dark" ? "manyselves-dark" : "manyselves-light"}
        value={tab.draftContent}
      />
    </div>
  );
}
