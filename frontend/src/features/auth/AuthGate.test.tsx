import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/providers";
import { AuthGate } from "./AuthGate";

describe("AuthGate", () => {
  it("shows the login page before bootstrapping when the session is absent", async () => {
    const api = {
      login: vi.fn(),
      logout: vi.fn(),
      session: vi.fn().mockRejectedValue(new Error("unauthorized")),
    };

    render(<AppProviders><AuthGate api={api}><div>Application content</div></AuthGate></AppProviders>);

    expect(await screen.findByRole("heading", { name: "登录 manyselves" })).toBeVisible();
    expect(screen.queryByText("Application content")).not.toBeInTheDocument();
  });

  it("renders its children only after a verified authenticated session", async () => {
    const api = {
      login: vi.fn(),
      logout: vi.fn(),
      session: vi.fn().mockResolvedValue({ authenticated: true, expiresAt: "2026-08-03T12:00:00Z", username: "admin" }),
    };

    render(<AppProviders><AuthGate api={api}><div>Application content</div></AuthGate></AppProviders>);

    expect(await screen.findByText("Application content")).toBeVisible();
  });
});
