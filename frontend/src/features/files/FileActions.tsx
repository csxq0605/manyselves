import { useRef, useState } from "react";

import type { LocalFileRef, PlatformBridge } from "../../platform/types";
import type { FileApi, FileEntry } from "./file-api";
import { UploadQueue, type UploadTask } from "./UploadQueue";

export interface FileActionsProps {
  readonly api: FileApi;
  readonly directory: string;
  readonly onChanged?: () => void;
  readonly onPreview?: ((entry: FileEntry) => void) | undefined;
  readonly onUploaded?: () => void;
  readonly platform: PlatformBridge;
  readonly projectId: string;
  readonly requestName?: (action: "create-file" | "create-directory" | "rename") => string | null;
  readonly selectedEntry?: FileEntry | null;
}

function joinPath(directory: string, name: string): string {
  return directory ? `${directory.replace(/\/$/, "")}/${name}` : name;
}

export function FileActions({
  api,
  directory,
  onChanged,
  onPreview,
  onUploaded,
  platform,
  projectId,
  requestName = (action) => window.prompt(
    action === "rename" ? "请输入新名称" : action === "create-directory" ? "请输入目录名称" : "请输入文件名称",
  ),
  selectedEntry = null,
}: FileActionsProps) {
  const [error, setError] = useState<string | null>(null);
  const [pendingDirectory, setPendingDirectory] = useState<{
    files: readonly LocalFileRef[];
    target: string;
  } | null>(null);
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const nextTaskId = useRef(0);
  const uploadFiles = useRef(new Map<
    string,
    { controller: AbortController; file: LocalFileRef; path: string }
  >());

  function updateTask(id: string, status: UploadTask["status"]) {
    setTasks((current) => current.map((task) => task.id === id ? { ...task, status } : task));
  }

  async function runUpload(id: string) {
    const work = uploadFiles.current.get(id);
    if (!work) return;
    updateTask(id, "uploading");
    try {
      await api.upload(
        projectId,
        work.path,
        work.file.file,
        "reject",
        undefined,
        work.controller.signal,
      );
      if (work.controller.signal.aborted) {
        updateTask(id, "cancelled");
        return;
      }
      updateTask(id, "completed");
      onUploaded?.();
      onChanged?.();
    } catch {
      updateTask(id, work.controller.signal.aborted ? "cancelled" : "failed");
    }
  }

  async function importFiles() {
    const selected = await platform.selectFiles();
    if (selected.length === 0) return;
    enqueueFiles(selected, directory);
  }

  function enqueueFiles(
    files: readonly LocalFileRef[],
    targetDirectory: string,
    preserveHierarchy = false,
  ) {
    for (const localFile of files) {
      const id = `upload-${nextTaskId.current++}`;
      const relativeName = preserveHierarchy && localFile.relativePath
        ? localFile.relativePath.split("/").slice(1).join("/") || localFile.name
        : localFile.name;
      uploadFiles.current.set(id, {
        controller: new AbortController(),
        file: localFile,
        path: joinPath(targetDirectory, relativeName),
      });
      setTasks((current) => [...current, { id, name: localFile.name, status: "queued" }]);
      void runUpload(id);
    }
  }

  async function selectDirectory() {
    const selected = await platform.selectDirectory();
    if (!selected || selected.files.length === 0) return;
    setPendingDirectory({ files: selected.files, target: joinPath(directory, selected.name) });
  }

  async function createEntry(kind: "file" | "directory") {
    const name = requestName(kind === "file" ? "create-file" : "create-directory")?.trim();
    if (!name) return;
    setError(null);
    try {
      await api.createEntry(projectId, { content: "", kind, path: joinPath(directory, name) });
      onChanged?.();
    } catch {
      setError(kind === "file" ? "文件创建失败" : "目录创建失败");
    }
  }

  async function renameSelected() {
    if (!selectedEntry) return;
    const name = requestName("rename")?.trim();
    if (!name) return;
    const parent = selectedEntry.path.split("/").slice(0, -1).join("/");
    setError(null);
    try {
      await api.renameEntry(projectId, {
        baseRevision: selectedEntry.revision,
        destination: joinPath(parent, name),
        source: selectedEntry.path,
      });
      onChanged?.();
    } catch {
      setError("重命名失败，服务器文件可能已更新");
    }
  }

  async function deleteSelected() {
    if (!selectedEntry || !window.confirm(`确定删除 ${selectedEntry.name}？`)) return;
    setError(null);
    try {
      await api.deleteEntry(projectId, selectedEntry.path, selectedEntry.revision);
      onChanged?.();
    } catch {
      setError("删除失败，服务器文件可能已更新");
    }
  }

  async function downloadSelected() {
    if (!selectedEntry || selectedEntry.kind !== "file") return;
    setError(null);
    try {
      const blob = await api.download(projectId, selectedEntry.path);
      const sourceUrl = api.downloadUrl?.(projectId, selectedEntry.path);
      await platform.saveDownload({
        blob,
        suggestedName: selectedEntry.name,
        ...(sourceUrl === undefined ? {} : { sourceUrl }),
      });
    } catch {
      setError("下载失败");
    }
  }

  return (
    <div className="file-actions">
      <button onClick={() => void importFiles()} type="button">导入文件</button>
      <button onClick={() => void selectDirectory()} type="button">导入目录</button>
      <button onClick={() => void createEntry("file")} type="button">新建文件</button>
      <button onClick={() => void createEntry("directory")} type="button">新建目录</button>
      <button disabled={!selectedEntry} onClick={() => void renameSelected()} type="button">重命名</button>
      <button disabled={!selectedEntry} onClick={() => void deleteSelected()} type="button">删除</button>
      <button
        disabled={!selectedEntry || selectedEntry.kind !== "file"}
        onClick={() => void downloadSelected()}
        type="button"
      >下载</button>
      <button
        disabled={!selectedEntry || selectedEntry.kind !== "file"}
        onClick={() => selectedEntry && onPreview?.(selectedEntry)}
        type="button"
      >预览</button>
      <button onClick={onChanged} type="button">刷新</button>
      {error ? <p role="alert">{error}</p> : null}
      {pendingDirectory ? (
        <div aria-label="目录导入确认" role="group">
          <p>{pendingDirectory.files.length} 个文件将导入到 {pendingDirectory.target}</p>
          <button
            onClick={() => {
              enqueueFiles(pendingDirectory.files, pendingDirectory.target, true);
              setPendingDirectory(null);
            }}
            type="button"
          >开始导入</button>
          <button onClick={() => setPendingDirectory(null)} type="button">取消</button>
        </div>
      ) : null}
      <UploadQueue
        onCancel={(id) => {
          uploadFiles.current.get(id)?.controller.abort();
          updateTask(id, "cancelled");
        }}
        onRetry={(id) => {
          const work = uploadFiles.current.get(id);
          if (!work) return;
          uploadFiles.current.set(id, { ...work, controller: new AbortController() });
          void runUpload(id);
        }}
        tasks={tasks}
      />
    </div>
  );
}
