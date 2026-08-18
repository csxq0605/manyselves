import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ComponentProps, StrictMode } from "react";
import { MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiGateway, type BootstrapSnapshot } from "../../api/gateway";
import { App } from "../../app/App";
import { AppProviders } from "../../app/providers";
import { AuthRequestError, type AuthApi, type SessionResponse } from "./auth-api";
import { useAuthenticatedSession } from "./auth-context";
import { AuthGate } from "./AuthGate";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

const authenticatedSession: SessionResponse = {
  authenticated: true,
  expiresAt: "2026-08-03T12:00:00Z",
  username: "admin",
};

const bootstrapSnapshot: BootstrapSnapshot = {
  agents: { main: "Main Agent" },
  conversations: [],
  maintenance: {},
  project: { id: "project-1" },
  runtime: {
    active_session_id: "session-1",
    agent_statuses: { main: "idle" },
    checkpoints: [],
    controller_client_id: null,
    debug: [],
    queues: [],
    ready: true,
    tasks: [],
    tools: [],
    workspace: null,
  },
  settings: { control_lease_seconds: 30, sse_client_queue_capacity: 128, sse_replay_capacity: 512 },
  streamId: "boot-a",
};

function gatewayWithBootstrap(bootstrap: ApiGateway["bootstrap"]): ApiGateway {
  return { baseUrl: "", bootstrap, clientId: "client" } as ApiGateway;
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

function SessionAwareApp({
  createEventStream,
  gateway,
}: Pick<ComponentProps<typeof App>, "createEventStream" | "gateway">) {
  const { returnToLogin } = useAuthenticatedSession();
  return <App gateway={gateway} onUnauthorized={returnToLogin} {...(createEventStream ? { createEventStream } : {})} />;
}

function renderGate(api: AuthApi, initialPath = "/") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    queryClient,
    ...render(
      <MemoryRouter initialEntries={[initialPath]}>
        <LocationProbe />
        <AppProviders queryClient={queryClient}>
          <AuthGate api={api}>
            <Routes>
              <Route path="/app/*" element={<div>Protected application</div>} />
              <Route path="*" element={<Navigate replace to="/app" />} />
            </Routes>
          </AuthGate>
        </AppProviders>
      </MemoryRouter>,
    ),
  };
}

describe("AuthGate session lifecycle", () => {
  it("does not bootstrap or start SSE while a StrictMode session check is pending", async () => {
    const pending = deferred<SessionResponse>();
    const api: AuthApi = { login: vi.fn(), logout: vi.fn(), session: vi.fn(() => pending.promise) };
    const bootstrap = vi.fn<() => Promise<BootstrapSnapshot>>().mockResolvedValue(bootstrapSnapshot);
    const start = vi.fn<() => Promise<void>>().mockResolvedValue();

    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/app"]}>
          <AppProviders>
            <AuthGate api={api}>
              <Routes>
                <Route path="/app/*" element={<SessionAwareApp createEventStream={() => ({ start, stop: vi.fn() })} gateway={gatewayWithBootstrap(bootstrap)} />} />
              </Routes>
            </AuthGate>
          </AppProviders>
        </MemoryRouter>
      </StrictMode>,
    );

    await waitFor(() => expect(api.session).toHaveBeenCalledTimes(1));
    expect(bootstrap).not.toHaveBeenCalled();
    expect(start).not.toHaveBeenCalled();
  });

  it("refreshes the session after login before mounting bootstrap and SSE", async () => {
    const api: AuthApi = {
      login: vi.fn().mockResolvedValue(undefined),
      logout: vi.fn(),
      session: vi.fn()
        .mockRejectedValueOnce(new AuthRequestError(401))
        .mockResolvedValueOnce(authenticatedSession),
    };
    const bootstrap = vi.fn<() => Promise<BootstrapSnapshot>>().mockResolvedValue(bootstrapSnapshot);
    const start = vi.fn<() => Promise<void>>().mockResolvedValue();
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/"]}>
        <AppProviders>
          <AuthGate api={api}>
            <Routes>
              <Route path="/app/*" element={<SessionAwareApp createEventStream={() => ({ start, stop: vi.fn() })} gateway={gatewayWithBootstrap(bootstrap)} />} />
              <Route path="*" element={<Navigate replace to="/app" />} />
            </Routes>
          </AuthGate>
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: /manyselves/i });
    expect(bootstrap).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText(/密码/), "password");
    await user.click(screen.getByRole("button", { name: /登录/ }));

    await waitFor(() => expect(bootstrap).toHaveBeenCalledOnce());
    await waitFor(() => expect(start).toHaveBeenCalledOnce());
    expect(api.login).toHaveBeenCalledWith({ password: "password", username: "admin" });
    expect(api.session).toHaveBeenCalledTimes(2);
  });

  it("returns an unauthenticated direct protected URL to the login route", async () => {
    const api: AuthApi = { login: vi.fn(), logout: vi.fn(), session: vi.fn().mockRejectedValue(new AuthRequestError(401)) };
    renderGate(api, "/app/projects/project-1");

    await screen.findByRole("heading", { name: /manyselves/i });
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/"));
    expect(screen.queryByText("Protected application")).not.toBeInTheDocument();
  });

  it("logs out at the server, clears protected cache, and replaces the route", async () => {
    const api: AuthApi = {
      login: vi.fn(),
      logout: vi.fn().mockResolvedValue(undefined),
      session: vi.fn().mockResolvedValueOnce(authenticatedSession).mockRejectedValueOnce(new AuthRequestError(401)),
    };
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(["bootstrap"], bootstrapSnapshot);

    function LogoutControl() {
      const { logout } = useAuthenticatedSession();
      return <button onClick={() => void logout()} type="button">Logout</button>;
    }

    render(
      <MemoryRouter initialEntries={["/app"]}>
        <LocationProbe />
        <AppProviders queryClient={queryClient}>
          <AuthGate api={api}>
            <Routes><Route path="/app/*" element={<LogoutControl />} /></Routes>
          </AuthGate>
        </AppProviders>
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Logout" }));

    await waitFor(() => expect(api.logout).toHaveBeenCalledOnce());
    await screen.findByRole("heading", { name: /manyselves/i });
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/"));
    expect(queryClient.getQueryData(["bootstrap"])).toBeUndefined();
  });

  it("returns to login when bootstrap receives a 401", async () => {
    const api: AuthApi = { login: vi.fn(), logout: vi.fn(), session: vi.fn().mockResolvedValue(authenticatedSession) };
    const bootstrap = vi.fn().mockRejectedValue(new ApiError({ code: "UNAUTHORIZED", details: {}, message: "expired", requestId: null, retryable: false, status: 401 }));
    render(
      <MemoryRouter initialEntries={["/app"]}>
        <AppProviders>
          <AuthGate api={api}>
            <Routes><Route path="/app/*" element={<SessionAwareApp gateway={gatewayWithBootstrap(bootstrap)} />} /></Routes>
          </AuthGate>
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: /manyselves/i });
    expect(bootstrap).toHaveBeenCalledOnce();
    expect(document.querySelector(".app-frame")).toBeNull();
  });

  it("returns to login when SSE reports unauthorized", async () => {
    const api: AuthApi = { login: vi.fn(), logout: vi.fn(), session: vi.fn().mockResolvedValue(authenticatedSession) };
    const start = vi.fn<() => Promise<void>>().mockResolvedValue();
    render(
      <MemoryRouter initialEntries={["/app"]}>
        <AppProviders>
          <AuthGate api={api}>
            <Routes>
              <Route path="/app/*" element={<SessionAwareApp createEventStream={(options) => ({ start: async () => { await start(); options.onStateChange?.("unauthorized"); }, stop: vi.fn() })} gateway={gatewayWithBootstrap(vi.fn().mockResolvedValue(bootstrapSnapshot))} />} />
            </Routes>
          </AuthGate>
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: /manyselves/i });
    expect(start).toHaveBeenCalledOnce();
  });
});
