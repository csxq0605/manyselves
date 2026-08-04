import { Outlet } from "react-router-dom";

import { Sidebar, type SidebarProps } from "./Sidebar";
import "./light-shell.css";

export type AppLayoutProps = SidebarProps;

export function AppLayout(props: AppLayoutProps) {
  return (
    <div className="light-app">
      <a className="skip-link" href="#main-outlet">跳转到主内容</a>
      <Sidebar {...props} />
      <main className="light-app__outlet" id="main-outlet"><Outlet /></main>
    </div>
  );
}
