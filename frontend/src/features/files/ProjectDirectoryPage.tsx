import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useParams } from "react-router-dom";

import { ApiError, type ApiGateway } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import { createEditorFileApi } from "../editor/editor-api";
import { createEditorStore } from "../editor/editor-store";
import { EditorWorkspace } from "../editor/EditorWorkspace";
import { createPreviewApi } from "../preview/preview-api";
import { PreviewWorkspace } from "../preview/PreviewWorkspace";
import { createFileApi, type FileApi, type FileEntry, type UploadConflict } from "./file-api";
import { FileList } from "./FileList";
import { OutputTabs } from "./OutputTabs";
import {
  isProjectFileSection,
  SECTION_CAPABILITIES,
  type SectionCapabilities,
} from "./project-sections";
import { UploadConflictDialog, type UploadConflictResolution } from "./UploadConflictDialog";
import "./project-directory.css";

export interface ProjectDirectoryPageProps {
  readonly api?: FileApi;
  readonly gateway: ApiGateway;
  readonly platform?: PlatformBridge;
}

interface ScopedProjectDirectoryPageProps extends ProjectDirectoryPageProps {
  readonly capabilities: SectionCapabilities;
  readonly projectId: string;
}

interface PendingConflict {
  readonly entry: FileEntry;
  readonly file: File;
  readonly path: string;
}

function joinPath(directory: string, name: string): string {
  return `${directory.replace(/\/$/, "")}/${name}`;
}

export function ProjectDirectoryPage({ api, gateway, platform }: ProjectDirectoryPageProps) {
  const { projectId, section } = useParams<{ projectId: string; section: string }>();
  const capabilities = isProjectFileSection(section) ? SECTION_CAPABILITIES[section] : null;
  if (!projectId || !capabilities) return <Navigate replace to="/" />;
  return (
    <ScopedProjectDirectoryPage
      capabilities={capabilities}
      gateway={gateway}
      key={`${projectId}\u0000${section}`}
      projectId={projectId}
      {...(api ? { api } : {})}
      {...(platform ? { platform } : {})}
    />
  );
}

function ScopedProjectDirectoryPage({
  api,
  capabilities,
  gateway,
  platform,
  projectId,
}: ScopedProjectDirectoryPageProps) {
  const resolvedApi = useMemo(() => api ?? createFileApi(gateway), [api, gateway]);
  const previewApi = useMemo(() => createPreviewApi(gateway), [gateway]);
  const editorApi = useMemo(() => createEditorFileApi(gateway), [gateway]);
  const editorStore = useMemo(() => createEditorStore(), []);
  const conflictResolver = useRef<((resolution: UploadConflictResolution) => void) | null>(null);
  const directoryInputRef = useRef<HTMLInputElement | null>(null);
  const disposedRef = useRef(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const lastUploadWasDirectoryRef = useRef(false);
  const directoryUploadButtonRef = useRef<HTMLButtonElement | null>(null);
  const uploadButtonRef = useRef<HTMLButtonElement | null>(null);
  const uploadControllerRef = useRef<AbortController | null>(null);
  const [conflict, setConflict] = useState<PendingConflict | null>(null);
  const [editing, setEditing] = useState(false);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const files = useQuery({
    queryFn: () => resolvedApi.listTree(projectId, capabilities.root),
    queryKey: ["files", projectId, capabilities.root],
  });
  useEffect(() => () => {
    disposedRef.current = true;
    uploadControllerRef.current?.abort();
    uploadControllerRef.current = null;
    conflictResolver.current?.("cancel");
    conflictResolver.current = null;
  }, []);

  const activeProjectId = projectId;
  const activeCapabilities = capabilities;

  async function sendUpload(
    file: File,
    mode: UploadConflict,
    path: string,
    signal: AbortSignal,
    baseRevision?: string,
  ) {
    await resolvedApi.upload(activeProjectId, path, file, mode, baseRevision, signal);
  }

  function requestConflictResolution(pending: PendingConflict): Promise<UploadConflictResolution> {
    return new Promise((resolve) => {
      conflictResolver.current = resolve;
      setConflict(pending);
    });
  }

  async function uploadOne(file: File, path: string, signal: AbortSignal) {
    setOperationError(null);
    try {
      await sendUpload(file, "reject", path, signal);
      if (signal.aborted) return;
      await files.refetch();
    } catch (reason) {
      if (signal.aborted || disposedRef.current) return;
      if (reason instanceof ApiError && reason.code === "FILE_ALREADY_EXISTS") {
        const refreshed = await files.refetch();
        if (signal.aborted || disposedRef.current) return;
        const entry = refreshed.isError
          ? undefined
          : refreshed.data?.find((candidate) => candidate.kind === "file" && candidate.path === path);
        if (entry) {
          const resolution = await requestConflictResolution({ entry, file, path });
          if (resolution === "cancel" || signal.aborted || disposedRef.current) return;
          try {
            await sendUpload(
              file,
              resolution,
              path,
              signal,
              resolution === "replace" ? entry.revision : undefined,
            );
            if (signal.aborted) return;
            await files.refetch();
          } catch {
            if (!signal.aborted && !disposedRef.current) {
              setOperationError("文件上传失败");
            }
          }
          return;
        }
      }
      setOperationError("文件上传失败");
    }
  }

  async function uploadSelected(selected: readonly File[], preserveDirectory = false) {
    if (selected.length === 0) return;
    const controller = new AbortController();
    uploadControllerRef.current = controller;
    setUploading(true);
    try {
      for (const file of selected) {
        if (disposedRef.current || controller.signal.aborted) break;
        const relativePath = preserveDirectory
          ? file.webkitRelativePath || file.name
          : file.name;
        await uploadOne(
          file,
          joinPath(activeCapabilities.root, relativePath),
          controller.signal,
        );
      }
    } finally {
      if (uploadControllerRef.current === controller) uploadControllerRef.current = null;
      if (!disposedRef.current) {
        setUploading(false);
        queueMicrotask(() => (
          lastUploadWasDirectoryRef.current
            ? directoryUploadButtonRef.current
            : uploadButtonRef.current
        )?.focus());
      }
    }
  }

  function resolveConflict(resolution: UploadConflictResolution) {
    const resolve = conflictResolver.current;
    conflictResolver.current = null;
    setConflict(null);
    resolve?.(resolution);
  }

  async function deleteFile(entry: FileEntry) {
    if (!window.confirm(`确定删除 ${entry.name}？`)) return;
    setOperationError(null);
    try {
      await resolvedApi.deleteEntry(activeProjectId, entry.path, entry.revision);
      await files.refetch();
    } catch {
      setOperationError("文件删除失败，服务器文件可能已更新");
    }
  }

  async function downloadFile(entry: FileEntry) {
    setOperationError(null);
    try {
      if (!platform) throw new Error("platform unavailable");
      const blob = await resolvedApi.download(activeProjectId, entry.path);
      await platform.saveDownload({
        blob,
        suggestedName: entry.kind === "directory" ? `${entry.name}.zip` : entry.name,
      });
    } catch {
      setOperationError(entry.kind === "directory" ? "文件夹下载失败" : "文件下载失败");
    }
  }

  async function editFile(entry: FileEntry) {
    setOperationError(null);
    try {
      const content = await editorApi.read(activeProjectId, entry.path);
      editorStore.getState().openFile(activeProjectId, content);
      setEditing(true);
    } catch {
      setOperationError("文件编辑器加载失败");
    }
  }

  return (
    <section className="project-directory">
      <header className="project-directory__header">
        <div>
          <p className="project-directory__eyebrow">{capabilities.root}/</p>
          <h1>{capabilities.label}</h1>
        </div>
        <div className="project-directory__actions">
          {capabilities.upload ? <>
            <input
              aria-label="选择本地文件"
              className="project-directory__file-input"
              multiple
              onChange={(event) => {
                const selected = Array.from(event.currentTarget.files ?? []);
                event.currentTarget.value = "";
                void uploadSelected(selected);
              }}
              ref={inputRef}
              type="file"
            />
            <button
              disabled={uploading}
              onClick={() => {
                lastUploadWasDirectoryRef.current = false;
                inputRef.current?.click();
              }}
              ref={uploadButtonRef}
              type="button"
            >
              {uploading ? "上传中…" : "上传本地文件"}
            </button>
            <input
              aria-label="选择本地文件夹"
              className="project-directory__file-input"
              multiple
              onChange={(event) => {
                const selected = Array.from(event.currentTarget.files ?? []);
                event.currentTarget.value = "";
                void uploadSelected(selected, true);
              }}
              ref={(element) => {
                directoryInputRef.current = element;
                element?.setAttribute("webkitdirectory", "");
              }}
              type="file"
            />
            <button
              disabled={uploading}
              onClick={() => {
                lastUploadWasDirectoryRef.current = true;
                directoryInputRef.current?.click();
              }}
              ref={directoryUploadButtonRef}
              type="button"
            >
              上传文件夹
            </button>
          </> : null}
          <button disabled={files.isFetching} onClick={() => void files.refetch()} type="button">刷新</button>
        </div>
      </header>

      {files.isPending ? <p role="status">正在加载文件…</p> : null}
      {files.isError ? <div><p role="alert">文件列表加载失败，请稍后重试。</p><button onClick={() => void files.refetch()} type="button">重新加载</button></div> : null}
      {operationError ? <p role="alert">{operationError}</p> : null}
      {files.data && files.data.length === 0 && !files.isError ? <p className="project-directory__empty">此目录还没有文件</p> : null}
      {files.data && files.data.length > 0 ? (
        capabilities.root === "Outputs" ? (
          <OutputTabs
            capabilities={capabilities}
            entries={files.data}
            onDelete={(entry) => void deleteFile(entry)}
            onDownload={(entry) => void downloadFile(entry)}
            onEdit={(entry) => void editFile(entry)}
            onPreview={(entry) => setPreviewPath(entry.path)}
          />
        ) : (
          <FileList
            capabilities={capabilities}
            entries={files.data}
            onDelete={(entry) => void deleteFile(entry)}
            onDownload={(entry) => void downloadFile(entry)}
            onEdit={(entry) => void editFile(entry)}
            onPreview={(entry) => setPreviewPath(entry.path)}
          />
        )
      ) : null}

      {previewPath && platform ? (
        <div className="project-directory__surface">
          <PreviewWorkspace api={previewApi} onClose={() => setPreviewPath(null)} path={previewPath} platform={platform} projectId={projectId} />
        </div>
      ) : null}
      {editing ? (
        <div className="project-directory__surface">
          <button className="project-directory__close" onClick={() => setEditing(false)} type="button">关闭编辑器</button>
          <EditorWorkspace
            api={editorApi}
            projectId={projectId}
            serverUrl={gateway.baseUrl}
            store={editorStore}
          />
        </div>
      ) : null}
      {conflict ? <UploadConflictDialog fileName={conflict.file.name} onResolve={resolveConflict} /> : null}
    </section>
  );
}
