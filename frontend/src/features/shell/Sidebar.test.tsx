import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/providers";
import type { Project } from "../projects/project-api";
import { Sidebar, type SidebarProps } from "./Sidebar";

const projects: Project[] = [{
  active: true,
  description: "Energy analysis",
  displayName: "Energy team",
  id: "energy-team",
  revision: "a".repeat(64),
}];

function renderSidebar(overrides: Partial<SidebarProps> = {}) {
  const props: SidebarProps = {
    onCreateProject: vi.fn().mockResolvedValue(projects[0]),
    onDeleteProject: vi.fn().mockResolvedValue(undefined),
    onUpdateProject: vi.fn().mockResolvedValue(projects[0]),
    projects,
    ...overrides,
  };
  return render(<AppProviders><MemoryRouter initialEntries={["/projects/energy-team/outputs"]}><Sidebar {...props} /></MemoryRouter></AppProviders>);
}

describe("Sidebar", () => {
  it("renders only the three top-level areas and every fixed project section", () => {
    renderSidebar({ accountUsername: "alice" });

    expect(screen.getByRole("link", { name: "新对话" })).toHaveAttribute("href", "/projects/energy-team/conversations/new");
    expect(screen.getByRole("link", { name: "全局知识库" })).toHaveAttribute("href", "/knowledge");
    expect(screen.getByRole("heading", { name: "项目" })).toBeVisible();
    expect(screen.queryByText("Agent 运行态")).not.toBeInTheDocument();
    for (const [name, suffix] of [["输入", "inputs"], ["知识库", "knowledge"], ["输出模板", "templates"], ["输出", "outputs"], ["工作流", "workflows"], ["运行态", "runtime"], ["日志", "logs"]] as const) {
      expect(screen.getByRole("link", { name })).toHaveAttribute("href", `/projects/energy-team/${suffix}`);
    }
    expect(screen.getByRole("button", { name: "编辑 Energy team" })).toBeVisible();
    expect(screen.getByRole("button", { name: "更多 Energy team" })).toBeVisible();
    expect(screen.getByText("alice")).toBeVisible();
    expect(screen.getByText("当前账户")).toBeVisible();
    expect(screen.getByRole("button", { name: "账户与设置" })).toBeVisible();
    expect(screen.queryByText("PROJECT WORKBENCH")).not.toBeInTheDocument();
  });

  it("updates metadata without changing a project's stable identifier", async () => {
    const update = vi.fn().mockResolvedValue(projects[0]);
    const user = userEvent.setup();
    renderSidebar({ onUpdateProject: update });

    await user.click(screen.getByRole("button", { name: "编辑 Energy team" }));
    await user.clear(screen.getByRole("textbox", { name: "显示名称" }));
    await user.type(screen.getByRole("textbox", { name: "显示名称" }), "Energy studio");
    await user.clear(screen.getByRole("textbox", { name: "描述（可选）" }));
    await user.type(screen.getByRole("textbox", { name: "描述（可选）" }), "New description");
    expect(screen.getByRole("textbox", { name: "项目标识" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(update).toHaveBeenCalledWith("energy-team", { description: "New description", displayName: "Energy studio", revision: "a".repeat(64) });
  });

  it("only deletes after the displayed name is typed exactly", async () => {
    const remove = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderSidebar({ onDeleteProject: remove });

    await user.click(screen.getByRole("button", { name: "更多 Energy team" }));
    await user.type(screen.getByRole("textbox"), "wrong");
    await user.click(screen.getByRole("button", { name: "删除项目" }));
    expect(remove).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toBeVisible();
    await user.clear(screen.getByRole("textbox"));
    await user.type(screen.getByRole("textbox"), "Energy team");
    await user.click(screen.getByRole("button", { name: "删除项目" }));

    expect(remove).toHaveBeenCalledWith("energy-team");
  });

  it("delegates a valid create to its explicit callback without requiring a ProjectApi", async () => {
    const onCreateProject = vi.fn().mockResolvedValue(projects[0]);
    const user = userEvent.setup();
    render(<AppProviders><MemoryRouter><Sidebar {...({ onCreateProject, onDeleteProject: vi.fn(), onUpdateProject: vi.fn(), projects } as ComponentProps<typeof Sidebar>)} /></MemoryRouter></AppProviders>);

    await user.click(screen.getByRole("button", { name: "新建项目" }));
    await user.type(screen.getByRole("textbox", { name: "项目标识" }), "new-project");
    await user.type(screen.getByRole("textbox", { name: "显示名称" }), "New project");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(onCreateProject).toHaveBeenCalledWith({ description: "", displayName: "New project", projectId: "new-project" });
  });

  it("does not submit a blank project identifier", async () => {
    const create = vi.fn().mockResolvedValue(projects[0]);
    const user = userEvent.setup();
    renderSidebar({ onCreateProject: create });

    await user.click(screen.getByRole("button", { name: "新建项目" }));
    await user.type(screen.getByRole("textbox", { name: "显示名称" }), "New project");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(create).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toBeVisible();
  });

  it("closes a project modal on Escape and restores focus to its invoking button", async () => {
    const user = userEvent.setup();
    renderSidebar();
    const invoke = screen.getByRole("button", { name: "编辑 Energy team" });

    await user.click(invoke);
    expect(screen.getByRole("dialog")).toBeVisible();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(invoke).toHaveFocus();
  });

  it("restores focus after a successful metadata save", async () => {
    const user = userEvent.setup();
    renderSidebar();
    const invoke = screen.getByRole("button", { name: "编辑 Energy team" });

    await user.click(invoke);
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(invoke).toHaveFocus();
  });
});
