import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type PropsWithChildren, useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { AuthRequestError, type AuthApi } from "./auth-api";
import { AuthenticatedSessionContext, type AuthenticatedSession } from "./auth-context";
import { LoginPage } from "./LoginPage";

export interface AuthGateProps extends PropsWithChildren {
  readonly api: AuthApi;
}

export function AuthGate({ api, children }: AuthGateProps) {
  const queryClient = useQueryClient();
  const location = useLocation();
  const navigate = useNavigate();
  const [sessionInvalidated, setSessionInvalidated] = useState(false);
  const session = useQuery({
    enabled: !sessionInvalidated,
    queryFn: () => api.session(),
    queryKey: ["auth", "session"],
    retry: (failureCount, error) =>
      !(error instanceof AuthRequestError && error.status === 401) && failureCount < 1,
    retryDelay: 50,
    staleTime: Infinity,
  });
  const returnToLogin = useCallback(() => {
    setSessionInvalidated(true);
    queryClient.clear();
    navigate("/", { replace: true });
  }, [navigate, queryClient]);
  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      returnToLogin();
    }
  }, [api, returnToLogin]);
  const login = useCallback(async (input: { username: string; password: string }) => {
    await api.login(input);
    await queryClient.fetchQuery({ queryFn: () => api.session(), queryKey: ["auth", "session"] });
    setSessionInvalidated(false);
  }, [api, queryClient]);
  const contextValue = useMemo<AuthenticatedSession | null>(() => {
    if (!session.data || sessionInvalidated) return null;
    return {
      logout,
      returnToLogin,
      session: session.data,
    };
  }, [logout, returnToLogin, session.data, sessionInvalidated]);
  useEffect(() => {
    if (!sessionInvalidated && !session.isPending && !session.data && (session.error instanceof AuthRequestError) && session.error.status === 401 && location.pathname !== "/") {
      navigate("/", { replace: true });
    }
  }, [location.pathname, navigate, session.data, session.error, session.isPending, sessionInvalidated]);

  if (sessionInvalidated) {
    return <LoginPage onLogin={login} />;
  }
  if (session.isPending) {
    return <main className="login-page" aria-busy="true" />;
  }
  if (contextValue) {
    return <AuthenticatedSessionContext value={contextValue}>{children}</AuthenticatedSessionContext>;
  }
  if (session.error && (!(session.error instanceof AuthRequestError) || session.error.status !== 401)) {
    return <main className="login-page"><section className="login-card"><p role="alert">无法连接到服务</p><button onClick={() => void session.refetch()} type="button">重试</button></section></main>;
  }
  return <LoginPage onLogin={login} />;
}
