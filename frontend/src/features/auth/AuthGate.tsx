import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type PropsWithChildren, useMemo } from "react";

import type { AuthApi } from "./auth-api";
import { AuthenticatedSessionContext, type AuthenticatedSession } from "./auth-context";
import { LoginPage } from "./LoginPage";

export interface AuthGateProps extends PropsWithChildren {
  readonly api: AuthApi;
}

export function AuthGate({ api, children }: AuthGateProps) {
  const queryClient = useQueryClient();
  const session = useQuery({
    queryFn: () => api.session(),
    queryKey: ["auth", "session"],
    retry: false,
    staleTime: Infinity,
  });
  const contextValue = useMemo<AuthenticatedSession | null>(() => {
    if (!session.data) return null;
    return {
      returnToLogin: () => queryClient.removeQueries({ queryKey: ["auth", "session"] }),
      session: session.data,
    };
  }, [queryClient, session.data]);

  if (session.isPending) {
    return <main className="login-page" aria-busy="true" />;
  }
  if (contextValue) {
    return <AuthenticatedSessionContext value={contextValue}>{children}</AuthenticatedSessionContext>;
  }
  return <LoginPage onLogin={async (input) => {
    await api.login(input);
    await queryClient.fetchQuery({ queryFn: () => api.session(), queryKey: ["auth", "session"] });
  }} />;
}
