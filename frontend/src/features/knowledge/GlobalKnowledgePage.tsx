import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, type ApiGateway } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import type { EditorFileApi } from "../editor/editor-api";
import { createEditorStore } from "../editor/editor-store";
import { EditorWorkspace } from "../editor/EditorWorkspace";
import type { FileEntry, UploadConflict } from "../files/file-api";
import { FileList } from "../files/FileList";
import type { SectionCapabilities } from "../files/project-sections";
import { UploadConflictDialog, type UploadConflictResolution } from "../files/UploadConflictDialog";
import type { PreviewApi } from "../preview/preview-api";
import { PreviewWorkspace } from "../preview/PreviewWorkspace";
import { createGlobalKnowledgeApi, type GlobalKnowledgeApi } from "./global-knowledge-api";
import "../files/project-directory.css";
import "./knowledge.css";

const GLOBAL_SCOPE_ID = "global-knowledge";
const capabilities: SectionCapabilities = {
  delete: true, download: true, edit: true, label: "全局知识库",
  preview: true, root: "", upload: true,
};

interface PendingConflict {
  readonly entry: FileEntry;
  readonly file: File;
  readonly path: string;
}

export interface GlobalKnowledgePageProps {
  readonly api?: GlobalKnowledgeApi;
  readonly gateway: ApiGateway;
  readonly platform?: PlatformBridge;
}

function isAlreadyExists(error: unknown): boolean {
  return error instanceof ApiError
    ? error.code === "FILE_ALREADY_EXISTS"
    : (error as { code?: unknown } | null)?.code === "FILE_ALREADY_EXISTS";
}

export function GlobalKnowledgePage({ api, gateway, platform }: GlobalKnowledgePageProps) {
  const resolvedApi = useMemo(() => api ?? createGlobalKnowledgeApi(gateway), [api, gateway]);
  const editorStore = useMemo(() => createEditorStore(), []);
  const editorApi = useMemo<EditorFileApi>(() => ({
    read: (_scope, path) => resolvedApi.read(path),
    save: (_scope, path, content, revision) => resolvedApi.save(path, content, revision),
  }), [resolvedApi]);
  const previewApi = useMemo<PreviewApi>(() => ({
    download: (url) => gateway.requestBlob(url),
    downloadRange: (url, begin, end) => resolvedApi.downloadRange(url, begin, end),
    get: (_scope, path) => resolvedApi.preview(path),
  }), [gateway, resolvedApi]);
  const conflictResolver = useRef<((resolution: UploadConflictResolution) => void) | null>(null);
  const disposed = useRef(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const uploadButtonRef = useRef<HTMLButtonElement | null>(null);
  const uploadController = useRef<AbortController | null>(null);
  const [conflict, setConflict] = useState<PendingConflict | null>(null);
  const [editing, setEditing] = useState(false);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const reportPreviewError = useCallback(() => {
    setOperationError("文件预览或解析失败，原文件已保留");
  }, []);
  const files = useQuery({
    queryFn: () => resolvedApi.listTree(),
    queryKey: ["global-knowledge", "files"],
  });

  useEffect(() => () => {
    disposed.current = true;
    uploadController.current?.abort();
    conflictResolver.current?.("cancel");
    conflictResolver.current = null;
  }, []);

  async function sendUpload(file: File, mode: UploadConflict, path: string, signal: AbortSignal, baseRevision?: string) {
    await resolvedApi.upload(path, file, mode, baseRevision, signal);
  }

  function requestConflict(pending: PendingConflict): Promise<UploadConflictResolution> {
    return new Promise((resolve) => {
      conflictResolver.current = resolve;
      setConflict(pending);
    });
  }

  async function uploadOne(file: File, signal: AbortSignal) {
    const path = file.name;
    setOperationError(null);
    try {
      await sendUpload(file, "reject", path, signal);
      if (!signal.aborted) await files.refetch();
    } catch (error) {
      if (signal.aborted || disposed.current) return;
      if (isAlreadyExists(error)) {
        const refreshed = await files.refetch();
        if (signal.aborted || disposed.current) return;
        const entry = refreshed.isError
          ? undefined
          : refreshed.data?.find((candidate) => candidate.kind === "file" && candidate.path === path);
        if (entry) {
          const resolution = await requestConflict({ entry, file, path });
          if (resolution === "cancel" || signal.aborted || disposed.current) return;
          try {
            await sendUpload(file, resolution, path, signal, resolution === "replace" ? entry.revision : undefined);
            if (!signal.aborted) await files.refetch();
          } catch {
            if (!signal.aborted && !disposed.current) setOperationError("文件上传失败，原文件已保留");
          }
          return;
        }
      }
      setOperationError("文件上传失败");
    }
  }

  async function uploadSelected(selected: readonly File[]) {
    if (!selected.length) return;
    const controller = new AbortController();
    uploadController.current = controller;
    setUploading(true);
    try {
      for (const file of selected) {
        if (controller.signal.aborted || disposed.current) break;
        await uploadOne(file, controller.signal);
      }
    } finally {
      if (uploadController.current === controller) uploadController.current = null;
      if (!disposed.current) {
        setUploading(false);
        queueMicrotask(() => uploadButtonRef.current?.focus());
      }
    }
  }

  function resolveConflict(resolution: UploadConflictResolution) {
    const resolve = conflictResolver.current;
    conflictResolver.current = null;
    setConflict(null);
    resolve?.(resolution);
  }

  async function editFile(entry: FileEntry) {
    setOperationError(null);
    try {
      const content = await resolvedApi.read(entry.path);
      editorStore.getState().openFile(GLOBAL_SCOPE_ID, content);
      setEditing(true);
    } catch {
      setOperationError("文件编辑器加载失败");
    }
  }

  async function deleteFile(entry: FileEntry) {
    if (!window.confirm(`确定删除 ${entry.name}？`)) return;
    setOperationError(null);
    try {
      await resolvedApi.deleteEntry(entry.path, entry.revision);
      if (previewPath === entry.path) setPreviewPath(null);
      await files.refetch();
    } catch {
      setOperationError("删除失败，文件可能已被其他操作更新");
    }
  }

  async function downloadFile(entry: FileEntry) {
    setOperationError(null);
    try {
      if (!platform) throw new Error("platform unavailable");
      const blob = await resolvedApi.download(entry.path);
      await platform.saveDownload({ blob, suggestedName: entry.name });
    } catch {
      setOperationError("文件下载失败");
    }
  }

  return <section className="global-knowledge project-directory">
    <header className="project-directory__header">
      <div><p className="project-directory__eyebrow">企业共享范围</p><h1>全局知识库</h1><p className="global-knowledge__summary">所有项目可引用；检索在运行时按需执行。</p></div>
      <div className="project-directory__actions">
        <input aria-label="选择本地文件" className="project-directory__file-input" multiple onChange={(event) => {
          const selected = Array.from(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
          void uploadSelected(selected);
        }} ref={inputRef} type="file" />
        <button disabled={uploading} onClick={() => inputRef.current?.click()} ref={uploadButtonRef} type="button">{uploading ? "上传中…" : "上传文件"}</button>
        <button disabled={files.isFetching} onClick={() => void files.refetch()} type="button">刷新</button>
      </div>
    </header>

    <div className="global-knowledge__state" role="status"><span>来源：企业全局知识</span><span>状态：运行时按需读取</span></div>
    {files.isPending ? <p role="status">正在加载全局知识…</p> : null}
    {files.isError ? <div><p role="alert">全局知识加载失败，请稍后重试。</p><button onClick={() => void files.refetch()} type="button">重新加载</button></div> : null}
    {operationError ? <p role="alert">{operationError}</p> : null}
    {files.data?.length === 0 && !files.isError ? <p className="project-directory__empty">全局知识库还没有文件</p> : null}
    {files.data?.length ? <FileList capabilities={capabilities} entries={files.data} onDelete={(entry) => void deleteFile(entry)} onDownload={(entry) => void downloadFile(entry)} onEdit={(entry) => void editFile(entry)} onPreview={(entry) => setPreviewPath(entry.path)} /> : null}

    {previewPath && platform ? <div className="project-directory__surface"><PreviewWorkspace api={previewApi} onClose={() => setPreviewPath(null)} onError={reportPreviewError} path={previewPath} platform={platform} projectId={GLOBAL_SCOPE_ID} /></div> : null}
    {editing ? <div className="project-directory__surface"><button className="project-directory__close" onClick={() => setEditing(false)} type="button">关闭编辑器</button><EditorWorkspace api={editorApi} projectId={GLOBAL_SCOPE_ID} serverUrl={gateway.baseUrl} store={editorStore} /></div> : null}
    {conflict ? <UploadConflictDialog fileName={conflict.file.name} onResolve={resolveConflict} /> : null}
  </section>;
}
