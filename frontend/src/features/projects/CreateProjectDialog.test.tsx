import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ProjectApi } from "./project-api";
import { CreateProjectDialog } from "./CreateProjectDialog";

function createdProject({ projectId, displayName, description }: Parameters<ProjectApi["create"]>[0]) {
  return { active: false, description, displayName, id: projectId ?? displayName, revision: "r1" };
}

describe("CreateProjectDialog", () => {
  it("creates a named project and reports it to the workspace", async () => {
    const created: string[] = [];
    const api: ProjectApi = {
      activate: async (projectId) => ({ active: true, description: "", displayName: projectId, id: projectId, revision: "r1" }),
      create: async (input) => createdProject(input),
      delete: async () => undefined,
      list: async () => [],
      update: async (projectId, input) => ({ active: false, ...input, id: projectId }),
    };
    const user = userEvent.setup();

    render(<CreateProjectDialog api={api} onCreated={(project) => created.push(project.id)} />);
    await user.click(screen.getByRole("button", { name: "新建项目" }));
    await user.type(screen.getByRole("textbox", { name: "项目标识" }), "energy-team");
    await user.type(screen.getByRole("textbox", { name: "显示名称" }), "Energy team");
    await user.type(screen.getByRole("textbox", { name: "描述（可选）" }), "Energy analysis");
    await user.click(screen.getByRole("button", { name: "创建" }));

    expect(created).toEqual(["energy-team"]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("sends the requested display metadata when creating", async () => {
    const create = vi.fn().mockResolvedValue({ active: false, description: "Energy analysis", displayName: "Energy team", id: "energy-team", revision: "r1" });
    const api: ProjectApi = { activate: async () => { throw new Error("unused"); }, create, delete: async () => undefined, list: async () => [], update: async () => { throw new Error("unused"); } };
    const user = userEvent.setup();

    render(<CreateProjectDialog api={api} onCreated={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "新建项目" }));
    await user.type(screen.getByRole("textbox", { name: "项目标识" }), "energy-team");
    await user.type(screen.getByRole("textbox", { name: "显示名称" }), "Energy team");
    await user.type(screen.getByRole("textbox", { name: "描述（可选）" }), "Energy analysis");
    await user.click(screen.getByRole("button", { name: "创建" }));

    expect(create).toHaveBeenCalledWith({ description: "Energy analysis", displayName: "Energy team", projectId: "energy-team" });
  });
});
