import { QueryClient } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../api/gateway";
import { AppRoutes } from "./routes";
import { AppProviders } from "./providers";

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function renderRoutes(
  savedRoute: string | null,
  options: { readonly conversations?: readonly { readonly sessionId: string }[]; readonly projectId?: string; readonly projects?: readonly { readonly active: boolean; readonly id: string }[] } = {},
) {
  window.localStorage.clear();
  if (savedRoute) window.localStorage.setItem("manyselves.lastProjectRoute.v1", savedRoute);
  const projects = options.projects ?? [{ active: true, id: "project-1" }];
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["bootstrap"], { conversations: options.conversations ?? [], project: { id: options.projectId ?? "project-1" } });
  const gateway = { requestJson: vi.fn().mockResolvedValue({ projects: projects.map((project) => ({ ...project, description: "", displayName: project.id, revision: "r1" })) }) } as unknown as ApiGateway;
  return render(<AppProviders queryClient={queryClient}><MemoryRouter initialEntries={["/"]}><AppRoutes gateway={gateway} /><LocationProbe /></MemoryRouter></AppProviders>);
}

describe("authenticated root route restore", () => {
  it("restores a saved project conversation when its project still exists", async () => {
    const view = renderRoutes("/projects/project-1/conversations/conversation-1", { conversations: [{ sessionId: "conversation-1" }] });

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/projects/project-1/conversations/conversation-1"));
  });

  it("falls back to the active project for malformed or stale saved routes", async () => {
    const view = renderRoutes("/settings/models");

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/projects/project-1"));
  });

  it("falls back to the active project for a saved project that no longer exists", async () => {
    const view = renderRoutes("/projects/deleted-project/conversations/conversation-1");

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/projects/project-1"));
  });

  it("restores the project home when the saved conversation was deleted or is the new placeholder", async () => {
    const deleted = renderRoutes("/projects/project-1/conversations/deleted", { conversations: [{ sessionId: "conversation-1" }] });
    await waitFor(() => expect(deleted.getByTestId("location")).toHaveTextContent("/projects/project-1"));
    deleted.unmount();
    const placeholder = renderRoutes("/projects/project-1/conversations/new", { conversations: [{ sessionId: "new" }] });
    await waitFor(() => expect(placeholder.getByTestId("location")).toHaveTextContent("/projects/project-1"));
  });

  it("restores the non-current project's home when its saved conversation cannot be validated", async () => {
    const view = renderRoutes("/projects/project-2/conversations/conversation-2", {
      conversations: [{ sessionId: "conversation-2" }],
      projectId: "project-1",
      projects: [{ active: true, id: "project-1" }, { active: false, id: "project-2" }],
    });

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/projects/project-2"));
  });

  it("waits for bootstrap conversation identity before replacing a saved conversation", async () => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "manyselves.lastProjectRoute.v1",
      "/projects/project-1/conversations/conversation-1",
    );
    let resolveBootstrap!: (value: unknown) => void;
    const bootstrap = new Promise((resolve) => { resolveBootstrap = resolve; });
    const gateway = {
      bootstrap: vi.fn(() => bootstrap),
      requestJson: vi.fn().mockResolvedValue({
        projects: [{
          active: true,
          description: "",
          displayName: "project-1",
          id: "project-1",
          revision: "r1",
        }],
      }),
    } as unknown as ApiGateway;
    const view = render(
      <AppProviders>
        <MemoryRouter initialEntries={["/"]}>
          <AppRoutes gateway={gateway} />
          <LocationProbe />
        </MemoryRouter>
      </AppProviders>,
    );

    await waitFor(() => expect(gateway.requestJson).toHaveBeenCalled());
    expect(view.getByTestId("location")).toHaveTextContent("/");
    resolveBootstrap({
      conversations: [{ sessionId: "conversation-1" }],
      project: { id: "project-1" },
    });

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/conversations/conversation-1",
    ));
  });
});
