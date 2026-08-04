import { Outlet } from "react-router-dom";

import type { ProjectApi } from "../projects/project-api";
import { Sidebar } from "./Sidebar";
import "./light-shell.css";

export interface AppLayoutProps { readonly onLogout?: () => void; readonly projectApi: ProjectApi; }

export function AppLayout({ onLogout, projectApi }: AppLayoutProps) {
  return (
    <div className="light-app">
      <Sidebar api={projectApi} {...(onLogout ? { onLogout } : {})} />
      <main className="light-app__outlet"><Outlet /></main>
    </div>
  );
}
