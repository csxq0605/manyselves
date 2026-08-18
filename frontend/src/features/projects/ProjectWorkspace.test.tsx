import { QueryClient } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/providers";
import type { ApiGateway } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import { ProjectWorkspace } from "./ProjectWorkspace";

describe("ProjectWorkspace", () => {
  it("loads project and file snapshots through the gateway", async () => {
    const requestJson = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") {
        return { projects: [{ active: true, id: "project-1" }] };
      }
      if (path.includes("/files/tree")) {
        return { entries: [{
          kind: "directory", modifiedAt: "2026-08-02T00:00:00Z", name: "Inputs",
          path: "Inputs", revision: "a".repeat(64), size: null,
        }] };
      }
      throw new Error(`unexpected ${path}`);
    });
    const gateway = {
      clientId: "browser-client",
      requestJson,
    } as unknown as ApiGateway;
    const platform = {
      kind: "browser",
      selectFiles: async () => [],
      selectDirectory: async () => null,
    } as unknown as PlatformBridge;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <AppProviders queryClient={queryClient}>
        <ProjectWorkspace
          activeProjectId="project-1"
          gateway={gateway}
          platform={platform}
        />
      </AppProviders>,
    );

    expect(await screen.findByRole("combobox", { name: "项目" })).toHaveValue("project-1");
    expect(await screen.findByRole("treeitem", { name: "Inputs" })).toBeVisible();
    expect(requestJson).toHaveBeenCalledWith("/api/v1/projects");
    expect(requestJson).toHaveBeenCalledWith("/api/v1/projects/project-1/files/tree");
  });
});
