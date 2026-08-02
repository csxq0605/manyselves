import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useStore } from "zustand";

import type { ApiGateway } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import { FileActions } from "../files/FileActions";
import { createFileApi, type FileEntry } from "../files/file-api";
import { createFileTreeStore } from "../files/file-tree-store";
import { FileTree } from "../files/FileTree";
import { CreateProjectDialog } from "./CreateProjectDialog";
import { createProjectApi } from "./project-api";
import { ProjectSwitcher } from "./ProjectSwitcher";

export interface ProjectWorkspaceProps {
  readonly activeProjectId: string;
  readonly gateway: ApiGateway;
  readonly hasDirtyDrafts?: boolean;
  readonly onProjectActivated?: (projectId: string) => void;
  readonly onOpenFile?: (entry: FileEntry) => void;
  readonly onPreviewFile?: ((entry: FileEntry) => void) | undefined;
  readonly platform: PlatformBridge;
}

const emptyExpandedPaths: readonly string[] = [];

function parentPath(path: string): string {
  return path.split("/").slice(0, -1).join("/");
}

export function ProjectWorkspace({
  activeProjectId,
  gateway,
  hasDirtyDrafts = false,
  onOpenFile,
  onPreviewFile,
  onProjectActivated,
  platform,
}: ProjectWorkspaceProps) {
  const queryClient = useQueryClient();
  const projectApi = useMemo(() => createProjectApi(gateway), [gateway]);
  const fileApi = useMemo(() => createFileApi(gateway), [gateway]);
  const [currentProjectId, setCurrentProjectId] = useState(activeProjectId);
  const [selectedEntry, setSelectedEntry] = useState<FileEntry | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [treeStore] = useState(() =>
    createFileTreeStore(typeof window === "undefined" ? undefined : window.localStorage),
  );
  const expandedPaths = useStore(
    treeStore,
    (state) => state.expandedByProject[currentProjectId] ?? emptyExpandedPaths,
  );

  const projects = useQuery({
    queryFn: () => projectApi.list(),
    queryKey: ["projects"],
  });
  const files = useQuery({
    enabled: currentProjectId.length > 0,
    queryFn: () => fileApi.listTree(currentProjectId),
    queryKey: ["files", currentProjectId],
  });

  function refreshFiles() {
    void queryClient.invalidateQueries({ queryKey: ["files", currentProjectId] });
  }

  async function renameEntry(entry: FileEntry) {
    const name = window.prompt("请输入新名称", entry.name)?.trim();
    if (!name || name === entry.name) return;
    setMutationError(null);
    try {
      await fileApi.renameEntry(currentProjectId, {
        baseRevision: entry.revision,
        destination: parentPath(entry.path) ? `${parentPath(entry.path)}/${name}` : name,
        source: entry.path,
      });
      refreshFiles();
    } catch {
      setMutationError("重命名失败，服务器文件可能已更新");
    }
  }

  async function deleteEntry(entry: FileEntry) {
    setMutationError(null);
    try {
      await fileApi.deleteEntry(currentProjectId, entry.path, entry.revision);
      setSelectedEntry(null);
      refreshFiles();
    } catch {
      setMutationError("删除失败，服务器文件可能已更新");
    }
  }

  const projectsForSwitcher = projects.data ?? [{ active: true, id: currentProjectId }];
  const directory = selectedEntry?.kind === "directory"
    ? selectedEntry.path
    : selectedEntry ? parentPath(selectedEntry.path) : "";

  return (
    <section className="project-workspace" aria-label="项目与文件管理">
      <div className="project-workspace__project-row">
        <ProjectSwitcher
          api={projectApi}
          current={currentProjectId}
          hasDirtyDrafts={hasDirtyDrafts}
          onActivated={(project) => {
            onProjectActivated?.(project.id);
            setCurrentProjectId(project.id);
            setSelectedEntry(null);
            queryClient.setQueryData(["project"], { id: project.id });
            void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
          }}
          projects={projectsForSwitcher}
        />
        <CreateProjectDialog
          api={projectApi}
          onCreated={(project) => {
            queryClient.setQueryData(
              ["projects"],
              [...(projects.data ?? []), project],
            );
            void queryClient.invalidateQueries({ queryKey: ["projects"] });
          }}
        />
      </div>
      <FileActions
        api={fileApi}
        directory={directory}
        onChanged={refreshFiles}
        onPreview={onPreviewFile}
        platform={platform}
        projectId={currentProjectId}
        selectedEntry={selectedEntry}
      />
      {projects.isError ? <p role="alert">项目列表加载失败</p> : null}
      {files.isPending ? <p role="status">正在加载服务器文件…</p> : null}
      {files.isError ? <p role="alert">文件列表加载失败</p> : null}
      {mutationError ? <p role="alert">{mutationError}</p> : null}
      {files.data ? (
        <FileTree
          confirmDelete={(entry) => window.confirm(`确定删除 ${entry.name}？`)}
          entries={files.data}
          initialExpanded={expandedPaths}
          key={currentProjectId}
          onDelete={(entry) => void deleteEntry(entry)}
          onExpandedChange={(paths) => treeStore.getState().setExpanded(currentProjectId, paths)}
          onOpen={(entry) => {
            setSelectedEntry(entry);
            if (entry.kind === "file") onOpenFile?.(entry);
          }}
          onRename={(entry) => void renameEntry(entry)}
          onSelect={setSelectedEntry}
          projectId={currentProjectId}
        />
      ) : null}
    </section>
  );
}
