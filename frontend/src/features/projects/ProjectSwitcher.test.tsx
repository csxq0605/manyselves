import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/gateway";
import type { ProjectApi } from "./project-api";
import { ProjectSwitcher } from "./ProjectSwitcher";

describe("ProjectSwitcher", () => {
  it("keeps the current project selected when activation is rejected", async () => {
    const activated: string[] = [];
    const api: ProjectApi = {
      activate: async () => {
        throw new ApiError({
          code: "RUNTIME_BUSY",
          details: {},
          message: "Runtime is busy",
          requestId: "request-1",
          retryable: false,
          status: 409,
        });
      },
      create: async (projectId) => ({ active: false, id: projectId }),
      list: async () => [],
    };
    const user = userEvent.setup();

    render(
      <ProjectSwitcher
        api={api}
        current="p1"
        onActivated={(project) => activated.push(project.id)}
        projects={[
          { active: true, id: "p1" },
          { active: false, id: "p2" },
        ]}
      />,
    );

    await user.selectOptions(screen.getByRole("combobox", { name: "项目" }), "p2");

    expect(screen.getByRole("combobox", { name: "项目" })).toHaveValue("p1");
    expect(screen.getByRole("alert")).toHaveTextContent("当前任务正在运行");
    expect(activated).toEqual([]);
  });

  it("asks before leaving a project with local drafts", async () => {
    const activated: string[] = [];
    const api: ProjectApi = {
      activate: async (projectId) => ({ active: true, id: projectId }),
      create: async (projectId) => ({ active: false, id: projectId }),
      list: async () => [],
    };
    const confirmActivation = vi.fn(() => false);
    const user = userEvent.setup();

    render(
      <ProjectSwitcher
        api={api}
        confirmActivation={confirmActivation}
        current="p1"
        hasDirtyDrafts
        onActivated={(project) => activated.push(project.id)}
        projects={[{ active: true, id: "p1" }, { active: false, id: "p2" }]}
      />,
    );

    await user.selectOptions(screen.getByRole("combobox", { name: "项目" }), "p2");

    expect(confirmActivation).toHaveBeenCalledOnce();
    expect(activated).toEqual([]);
  });
});
