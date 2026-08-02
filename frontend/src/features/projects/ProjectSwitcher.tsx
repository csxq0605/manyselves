import { useState } from "react";

import { ApiError } from "../../api/gateway";
import type { Project, ProjectApi } from "./project-api";

export interface ProjectSwitcherProps {
  readonly api: ProjectApi;
  readonly confirmActivation?: () => boolean;
  readonly current: string;
  readonly hasDirtyDrafts?: boolean;
  readonly onActivated: (project: Project) => void;
  readonly projects: readonly Project[];
}

function activationErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === "RUNTIME_BUSY") {
    return "当前任务正在运行，暂时不能切换项目";
  }
  if (error instanceof ApiError) {
    return error.message;
  }
  return "项目切换失败，请稍后重试";
}

export function ProjectSwitcher({
  api,
  confirmActivation = () => window.confirm("当前项目有未保存草稿，仍要切换项目吗？"),
  current,
  hasDirtyDrafts = false,
  onActivated,
  projects,
}: ProjectSwitcherProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function activate(projectId: string) {
    if (projectId === current || pending) {
      return;
    }
    if (hasDirtyDrafts && !confirmActivation()) {
      return;
    }
    setPending(true);
    setError(null);
    try {
      onActivated(await api.activate(projectId));
    } catch (reason) {
      setError(activationErrorMessage(reason));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="project-switcher">
      <label>
        <span className="pane-label">项目</span>
        <select
          aria-label="项目"
          disabled={pending}
          onChange={(event) => void activate(event.target.value)}
          value={current}
        >
          {projects.map((project) => (
            <option key={project.id} value={project.id}>{project.id}</option>
          ))}
        </select>
      </label>
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}
