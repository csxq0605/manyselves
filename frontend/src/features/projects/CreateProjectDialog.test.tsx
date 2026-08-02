import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { ProjectApi } from "./project-api";
import { CreateProjectDialog } from "./CreateProjectDialog";

describe("CreateProjectDialog", () => {
  it("creates a named project and reports it to the workspace", async () => {
    const created: string[] = [];
    const api: ProjectApi = {
      activate: async (projectId) => ({ active: true, id: projectId }),
      create: async (projectId) => ({ active: false, id: projectId }),
      list: async () => [],
    };
    const user = userEvent.setup();

    render(<CreateProjectDialog api={api} onCreated={(project) => created.push(project.id)} />);
    await user.click(screen.getByRole("button", { name: "新建项目" }));
    await user.type(screen.getByRole("textbox", { name: "项目标识" }), "energy-team");
    await user.click(screen.getByRole("button", { name: "创建" }));

    expect(created).toEqual(["energy-team"]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
