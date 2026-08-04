import { Outlet, useLocation } from "react-router-dom";

import { Sidebar, type SidebarProps } from "./Sidebar";
import "./light-shell.css";

export type AppLayoutProps = SidebarProps;

export function AppLayout(props: AppLayoutProps) {
  const location = useLocation();
  const conversationRoute = /\/projects\/[^/]+\/conversations\//.test(location.pathname);
  return (
    <div className="light-app">
      <a className="skip-link" href="#main-outlet">跳转到主内容</a>
      <Sidebar {...props} />
      <main className={`light-app__outlet${conversationRoute ? " light-app__outlet--conversation" : ""}`} id="main-outlet" tabIndex={-1}><Outlet /></main>
    </div>
  );
}
