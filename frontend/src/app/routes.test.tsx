import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../api/gateway";
import { AppRoutes } from "./routes";
import { AppProviders } from "./providers";

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function renderRoutes(savedRoute: string | null) {
  window.localStorage.clear();
  if (savedRoute) window.localStorage.setItem("manyselves.lastProjectRoute.v1", savedRoute);
  const gateway = { requestJson: vi.fn().mockResolvedValue({ projects: [{ active: true, description: "", displayName: "Project one", id: "project-1", revision: "r1" }] }) } as unknown as ApiGateway;
  return render(<AppProviders><MemoryRouter initialEntries={["/"]}><AppRoutes gateway={gateway} /><LocationProbe /></MemoryRouter></AppProviders>);
}

describe("authenticated root route restore", () => {
  it("restores a saved project conversation when its project still exists", async () => {
    const view = renderRoutes("/projects/project-1/conversations/conversation-1");

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
});
