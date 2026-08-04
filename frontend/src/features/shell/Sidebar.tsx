import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../../api/gateway";
import type { Project, ProjectCreateInput, ProjectUpdateInput } from "../projects/project-api";

export type ProjectSection = "inputs" | "knowledge" | "templates" | "outputs" | "runtime" | "logs";

type IconName = "archive" | "book" | "edit" | "file-out" | "folder" | "knowledge" | "logs" | "more" | "output" | "plus" | "runtime" | "settings";

const sections: readonly (readonly [ProjectSection, string, IconName])[] = [
  ["inputs", "输入", "archive"], ["knowledge", "知识库", "book"], ["templates", "输出模板", "file-out"],
  ["outputs", "输出", "output"], ["runtime", "运行态", "runtime"], ["logs", "日志", "logs"],
];

function Icon({ name }: { readonly name: IconName }) {
  const common = { fill: "none", stroke: "currentColor", strokeLinecap: "round" as const, strokeLinejoin: "round" as const, strokeWidth: 1.7 };
  return <svg aria-hidden="true" className="sidebar-icon" viewBox="0 0 24 24">
    {name === "edit" ? <><path {...common} d="M4 20h4l11-11a2.8 2.8 0 0 0-4-4L4 16v4Z" /><path {...common} d="m13.5 6.5 4 4" /></> : null}
    {name === "knowledge" ? <><path {...common} d="M5 4v16M10 4v16M15 5l4 14" /><path {...common} d="M3 4h4M8 4h4M14 6l4-1" /></> : null}
    {name === "folder" ? <path {...common} d="M3 6.5h7l2 2h9v10.5H3V6.5Z" /> : null}
    {name === "archive" ? <><path {...common} d="M4 9h16v10H4zM3 5h18v4H3z" /><path {...common} d="M9 13h6" /></> : null}
    {name === "book" ? <><path {...common} d="M3 5.5c3-1.4 6-1 9 1v13c-3-2-6-2.4-9-1V5.5Z" /><path {...common} d="M21 5.5c-3-1.4-6-1-9 1v13c3-2 6-2.4 9-1V5.5Z" /></> : null}
    {name === "file-out" ? <><path {...common} d="M7 3h8l4 4v14H7zM15 3v5h4" /><path {...common} d="M3 13h9M6 10l-3 3 3 3" /></> : null}
    {name === "output" ? <><path {...common} d="M6 3h9l4 4v14H6zM15 3v5h4" /><path {...common} d="m9 14 2 2 4-4" /></> : null}
    {name === "runtime" ? <path {...common} d="M3 12h4l2.5-7 5 14 2.5-7h4" /> : null}
    {name === "logs" ? <><path {...common} d="M6 4h12v16H6z" /><path {...common} d="M9 8h6M9 12h6M9 16h4M3 7h3M3 11h3M3 15h3" /></> : null}
    {name === "plus" ? <path {...common} d="M12 5v14M5 12h14" /> : null}
    {name === "more" ? <><circle cx="5" cy="12" fill="currentColor" r="1.4" /><circle cx="12" cy="12" fill="currentColor" r="1.4" /><circle cx="19" cy="12" fill="currentColor" r="1.4" /></> : null}
    {name === "settings" ? <><circle {...common} cx="12" cy="12" r="3" /><path {...common} d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1M18.7 18.7l-2.1-2.1M7.4 7.4 5.3 5.3" /></> : null}
  </svg>;
}

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
  const location = useLocation();
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
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);

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
  const routeProjectId = (() => {
    const match = /^\/projects\/([^/]+)/.exec(location.pathname);
    if (!match?.[1]) return home;
    try { return decodeURIComponent(match[1]); } catch { return home; }
  })();

  return (
    <aside className="sidebar" aria-label="项目导航">
      <nav aria-label="主导航" className="sidebar__top">
        <Link to={routeProjectId ? `/projects/${encodeURIComponent(routeProjectId)}/conversations/new` : "/"}><Icon name="edit" /><span>新对话</span></Link>
        <NavLink to="/knowledge"><Icon name="knowledge" /><span>全局知识库</span></NavLink>
      </nav>
      <section className="sidebar__projects" aria-labelledby="projects-title">
        <div className="sidebar__section-heading"><h2 id="projects-title">项目</h2><button aria-label="新建项目" onClick={(event) => open("create", undefined, event.currentTarget)} type="button"><Icon name="plus" /></button></div>
        {projectsError ? <p aria-live="polite" role="alert">项目列表加载失败</p> : null}
        <ul>{projects.map((project) => <li className="project-node" key={project.id}>
          <div className={`project-node__row${routeProjectId === project.id ? " project-node__row--selected" : ""}`}><NavLink end to={`/projects/${encodeURIComponent(project.id)}`}><Icon name="folder" /><span>{project.displayName}</span></NavLink>
            <button aria-label={`编辑 ${project.displayName}`} onClick={(event) => open("edit", project, event.currentTarget)} type="button"><Icon name="edit" /></button>
            <button aria-label={`更多 ${project.displayName}`} onClick={(event) => open("delete", project, event.currentTarget)} type="button"><Icon name="more" /></button>
          </div>
          {routeProjectId === project.id ? <ul className="project-node__sections">{sections.map(([section, label, icon]) => <li key={section}><NavLink to={`/projects/${encodeURIComponent(project.id)}/${section}`}><Icon name={icon} /><span>{label}</span></NavLink></li>)}</ul> : null}
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
      <div className="sidebar__account">
        <span aria-hidden="true" className="sidebar__avatar">A</span>
        <span className="sidebar__identity"><strong>admin</strong><small>系统管理员</small></span>
        <div className="sidebar__account-menu">
          <button aria-expanded={accountMenuOpen} aria-label="账户与设置" onClick={() => setAccountMenuOpen((current) => !current)} type="button"><Icon name="settings" /></button>
          {accountMenuOpen ? <div><NavLink to="/settings/models">模型设置</NavLink>{onLogout ? <button onClick={onLogout} type="button">退出登录</button> : null}</div> : null}
        </div>
      </div>
    </aside>
  );
}
