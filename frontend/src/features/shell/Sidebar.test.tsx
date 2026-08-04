import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/providers";
import type { Project, ProjectApi } from "../projects/project-api";
import { Sidebar } from "./Sidebar";

const projects: Project[] = [{
  active: true,
  description: "Energy analysis",
  displayName: "Energy team",
  id: "energy-team",
  revision: "a".repeat(64),
}];

function renderSidebar(api?: ProjectApi) {
  return render(<AppProviders><MemoryRouter initialEntries={["/projects/energy-team/outputs"]}><Sidebar projects={projects} {...(api ? { api } : {})} /></MemoryRouter></AppProviders>);
}

describe("Sidebar", () => {
  it("renders only the three top-level areas and every fixed project section", () => {
    renderSidebar();

    expect(screen.getByRole("link", { name: "新对话" })).toHaveAttribute("href", "/projects/energy-team");
    expect(screen.getByRole("link", { name: "全局知识库" })).toHaveAttribute("href", "/knowledge");
    expect(screen.getByRole("heading", { name: "项目" })).toBeVisible();
    expect(screen.queryByText("Agent 运行态")).not.toBeInTheDocument();
    for (const [name, suffix] of [["输入", "inputs"], ["知识库", "knowledge"], ["输出模板", "templates"], ["输出", "outputs"], ["运行态", "runtime"], ["日志", "logs"]] as const) {
      expect(screen.getByRole("link", { name })).toHaveAttribute("href", `/projects/energy-team/${suffix}`);
    }
    expect(screen.getByRole("button", { name: "编辑 Energy team" })).toBeVisible();
    expect(screen.getByRole("button", { name: "更多 Energy team" })).toBeVisible();
  });

  it("updates metadata without changing a project's stable identifier", async () => {
    const update = vi.fn().mockResolvedValue(projects[0]);
    const api: ProjectApi = { activate: vi.fn(), create: vi.fn(), delete: vi.fn(), list: vi.fn().mockResolvedValue(projects), update };
    const user = userEvent.setup();
    renderSidebar(api);

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
    const api: ProjectApi = { activate: vi.fn(), create: vi.fn(), delete: remove, list: vi.fn().mockResolvedValue(projects), update: vi.fn() };
    const user = userEvent.setup();
    renderSidebar(api);

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
});
