import { type FormEvent, useState } from "react";

import { ApiError } from "../../api/gateway";
import type { Project, ProjectApi } from "./project-api";

export interface CreateProjectDialogProps {
  readonly api: ProjectApi;
  readonly onCreated: (project: Project) => void;
}

export function CreateProjectDialog({ api, onCreated }: CreateProjectDialogProps) {
  const [open, setOpen] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const normalizedId = projectId.trim();
    if (!normalizedId) {
      setError("请输入项目标识");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.create(normalizedId);
      onCreated(created);
      setOpen(false);
      setProjectId("");
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "项目创建失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <button onClick={() => setOpen(true)} type="button">新建项目</button>
      {open ? (
        <div aria-labelledby="create-project-title" aria-modal="true" role="dialog">
          <form onSubmit={(event) => void submit(event)}>
            <h3 id="create-project-title">新建项目</h3>
            <label>
              项目标识
              <input
                aria-label="项目标识"
                autoFocus
                onChange={(event) => setProjectId(event.target.value)}
                value={projectId}
              />
            </label>
            {error ? <p role="alert">{error}</p> : null}
            <button disabled={submitting} type="submit">创建</button>
            <button disabled={submitting} onClick={() => setOpen(false)} type="button">取消</button>
          </form>
        </div>
      ) : null}
    </>
  );
}
