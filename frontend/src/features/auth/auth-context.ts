import { createContext, useContext } from "react";

import type { SessionResponse } from "./auth-api";

export interface AuthenticatedSession {
  readonly returnToLogin: () => void;
  readonly session: SessionResponse;
}

export const AuthenticatedSessionContext = createContext<AuthenticatedSession | null>(null);

export function useAuthenticatedSession(): AuthenticatedSession {
  const value = useContext(AuthenticatedSessionContext);
  if (!value) {
    throw new Error("Authenticated session is required");
  }
  return value;
}
