import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";

import { ApiError } from "../../api/gateway";
import type { Project, ProjectCreateInput, ProjectUpdateInput } from "../projects/project-api";

export type ProjectSection = "inputs" | "knowledge" | "templates" | "outputs" | "runtime" | "logs";

const sections: readonly (readonly [ProjectSection, string])[] = [
  ["inputs", "输入"], ["knowledge", "知识库"], ["templates", "输出模板"],
  ["outputs", "输出"], ["runtime", "运行态"], ["logs", "日志"],
];

export interface SidebarProps {
  readonly onCreateProject: (input: ProjectCreateInput) => Promise<Project>;
  readonly onDeleteProject: (projectId: string) => Promise<void>;
  readonly onLogout?: () => void;
  readonly onUpdateProject: (projectId: string, input: ProjectUpdateInput) => Promise<Project>;
  readonly projects: readonly Project[];
  readonly projectsError?: boolean;
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "项目操作失败，请稍后重试";
}

export function Sidebar({ onCreateProject, onDeleteProject, onLogout, onUpdateProject, projects, projectsError = false }: SidebarProps) {
  const navigate = useNavigate();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const invokerRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocus = useRef(false);
  const [mode, setMode] = useState<"create" | "edit" | "delete" | null>(null);
  const [selected, setSelected] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [confirmation, setConfirmation] = useState("");

  function open(nextMode: "create" | "edit" | "delete", project: Project | undefined, invoker: HTMLButtonElement) {
    invokerRef.current = invoker;
    setMode(nextMode); setSelected(project ?? null); setError(null); setPending(false); setConfirmation("");
    setProjectId(project?.id ?? ""); setDisplayName(project?.displayName ?? ""); setDescription(project?.description ?? "");
  }
  function close() {
    if (pending) return;
    restoreFocus.current = true;
    setMode(null);
  }
  useEffect(() => {
    if (!mode && restoreFocus.current) {
      restoreFocus.current = false;
      invokerRef.current?.focus();
    }
  }, [mode]);
  useEffect(() => {
    if (!mode) return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), textarea:not([disabled])"));
    const focusInitial = () => {
      if (window.matchMedia?.("(max-width: 700px)").matches) return;
      focusable()[0]?.focus();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!pending) {
          restoreFocus.current = true;
          setMode(null);
        }
        return;
      }
      if (event.key !== "Tab") return;
      const targets = focusable();
      const first = targets[0];
      const last = targets.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    const keepFocusInside = (event: FocusEvent) => {
      if (event.target instanceof Node && !dialog.contains(event.target)) focusable()[0]?.focus();
    };
    queueMicrotask(focusInitial);
    document.addEventListener("focusin", keepFocusInside);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("focusin", keepFocusInside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [mode, pending]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    const normalizedProjectId = projectId.trim();
    const normalizedDisplayName = displayName.trim();
    if (mode !== "delete" && (!normalizedProjectId || !normalizedDisplayName)) {
      setError("请填写项目标识和显示名称");
      return;
    }
    if (mode === "delete" && selected && confirmation !== selected.displayName) {
      setError("请输入项目显示名称以确认删除");
      return;
    }
    setPending(true); setError(null);
    try {
      if (mode === "create") {
        const created = await onCreateProject({ description: description.trim(), displayName: normalizedDisplayName, projectId: normalizedProjectId });
        navigate(`/projects/${encodeURIComponent(created.id)}`);
      } else if (mode === "edit" && selected) {
        await onUpdateProject(selected.id, { description: description.trim(), displayName: normalizedDisplayName, revision: selected.revision });
      } else if (mode === "delete" && selected) {
        await onDeleteProject(selected.id);
        navigate("/");
      }
      restoreFocus.current = true;
      setMode(null);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setPending(false);
    }
  }
  const home = projects.find((project) => project.active)?.id ?? projects[0]?.id ?? "";

  return (
    <aside className="sidebar" aria-label="项目导航">
      <div className="sidebar__brand"><span>manyselves</span><small>PROJECT WORKBENCH</small></div>
      <nav aria-label="主导航" className="sidebar__top">
        <Link to={home ? `/projects/${encodeURIComponent(home)}` : "/"}>新对话</Link>
        <NavLink to="/knowledge">全局知识库</NavLink>
      </nav>
      <section className="sidebar__projects" aria-labelledby="projects-title">
        <div className="sidebar__section-heading"><h2 id="projects-title">项目</h2><button aria-label="新建项目" onClick={(event) => open("create", undefined, event.currentTarget)} type="button">+</button></div>
        {projectsError ? <p aria-live="polite" role="alert">项目列表加载失败</p> : null}
        <ul>{projects.map((project) => <li className="project-node" key={project.id}>
          <div className="project-node__row"><NavLink end to={`/projects/${encodeURIComponent(project.id)}`}>{project.displayName}</NavLink>
            <button aria-label={`编辑 ${project.displayName}`} onClick={(event) => open("edit", project, event.currentTarget)} type="button">✎</button>
            <button aria-label={`更多 ${project.displayName}`} onClick={(event) => open("delete", project, event.currentTarget)} type="button">⋯</button>
          </div>
          <ul className="project-node__sections">{sections.map(([section, label]) => <li key={section}><NavLink to={`/projects/${encodeURIComponent(project.id)}/${section}`}>{label}</NavLink></li>)}</ul>
        </li>)}</ul>
      </section>
      {mode ? <div aria-labelledby="project-dialog-title" aria-modal="true" className="project-dialog" ref={dialogRef} role="dialog"><form onSubmit={(event) => void submit(event)}>
        <h3 id="project-dialog-title">{mode === "create" ? "新建项目" : mode === "edit" ? "编辑项目" : "删除项目"}</h3>
        {mode === "delete" ? <label>输入“{selected?.displayName}”确认删除<input autoComplete="off" name="confirmation" onChange={(event) => setConfirmation(event.target.value)} value={confirmation} /></label> : <>
          <label>项目标识<input autoComplete="off" disabled={mode === "edit"} name="projectId" onChange={(event) => setProjectId(event.target.value)} value={projectId} /></label>
          <label>显示名称<input autoComplete="off" name="displayName" onChange={(event) => setDisplayName(event.target.value)} value={displayName} /></label>
          <label>描述（可选）<textarea autoComplete="off" name="description" onChange={(event) => setDescription(event.target.value)} value={description} /></label>
        </>}
        {error ? <p aria-live="polite" role="alert">{error}</p> : null}<button disabled={pending} type="submit">{mode === "delete" ? "删除项目" : "保存"}</button><button disabled={pending} onClick={close} type="button">取消</button>
      </form></div> : null}
      {onLogout ? <button className="sidebar__logout" onClick={onLogout} type="button">退出登录</button> : null}
    </aside>
  );
}
