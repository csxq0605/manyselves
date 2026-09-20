import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/providers";
import { AppLayout } from "./AppLayout";

describe("AppLayout", () => {
  it("composes the navigation rail with its routed outlet", () => {
    render(<AppProviders><MemoryRouter initialEntries={["/projects/p1"]}><Routes><Route element={<AppLayout onCreateProject={vi.fn()} onDeleteProject={vi.fn()} onUpdateProject={vi.fn()} projects={[]} />}><Route path="/projects/:projectId" element={<h1>Project outlet</h1>} /></Route></Routes></MemoryRouter></AppProviders>);

    expect(screen.getByRole("navigation", { name: "主导航" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Project outlet" })).toBeVisible();
  });

  it("keeps the sidebar mounted on a Main conversation route", () => {
    render(<AppProviders><MemoryRouter initialEntries={["/projects/p1/conversations/s1"]}><Routes><Route element={<AppLayout onCreateProject={vi.fn()} onDeleteProject={vi.fn()} onUpdateProject={vi.fn()} projects={[]} />}><Route path="/projects/:projectId/conversations/:sessionId" element={<h1>Main conversation</h1>} /></Route></Routes></MemoryRouter></AppProviders>);

    expect(screen.getByRole("navigation", { name: "主导航" })).toBeVisible();
    expect(screen.getByLabelText("项目导航")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Main conversation" })).toBeVisible();
  });
});
