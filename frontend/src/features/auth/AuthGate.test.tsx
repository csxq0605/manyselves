import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/providers";
import { AuthRequestError } from "./auth-api";
import { AuthGate } from "./AuthGate";

describe("AuthGate", () => {
  it("shows the login page before bootstrapping when the session is absent", async () => {
    const api = {
      login: vi.fn(),
      logout: vi.fn(),
      session: vi.fn().mockRejectedValue(new AuthRequestError(401)),
    };

    render(<MemoryRouter initialEntries={["/app"]}><AppProviders><AuthGate api={api}><div>Application content</div></AuthGate></AppProviders></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "登录 manyselves" })).toBeVisible();
    expect(screen.queryByText("Application content")).not.toBeInTheDocument();
  });

  it("renders its children only after a verified authenticated session", async () => {
    const api = {
      login: vi.fn(),
      logout: vi.fn(),
      session: vi.fn().mockResolvedValue({ authenticated: true, expiresAt: "2026-08-03T12:00:00Z", username: "admin" }),
    };

    render(<MemoryRouter><AppProviders><AuthGate api={api}><div>Application content</div></AuthGate></AppProviders></MemoryRouter>);

    expect(await screen.findByText("Application content")).toBeVisible();
  });

  it("shows a retryable connection failure instead of treating a server error as a login rejection", async () => {
    const api = { login: vi.fn(), logout: vi.fn(), session: vi.fn().mockRejectedValue(new Error("server unavailable")) };
    render(<MemoryRouter><AppProviders><AuthGate api={api}><div>Application content</div></AuthGate></AppProviders></MemoryRouter>);

    expect(await screen.findByRole("alert")).toHaveTextContent("无法连接到服务");
    expect(screen.getByRole("button", { name: "重试" })).toBeVisible();
  });
});
