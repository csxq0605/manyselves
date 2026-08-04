import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { ProjectHomePage } from "./ProjectHomePage";

describe("ProjectHomePage", () => {
  it("keeps conversations on the project home rather than in the directory tree", () => {
    render(
      <MemoryRouter initialEntries={["/projects/energy-team"]}>
        <Routes><Route path="/projects/:projectId" element={<ProjectHomePage />} /></Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "最近对话" })).toBeVisible();
    expect(screen.getByRole("button", { name: "新对话" })).toBeVisible();
    expect(screen.queryByRole("tree")).not.toBeInTheDocument();
  });
});
