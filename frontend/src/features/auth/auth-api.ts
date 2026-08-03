export interface SessionResponse {
  readonly authenticated: true;
  readonly expiresAt: string;
  readonly username: string;
}

export interface AuthApi {
  session(): Promise<SessionResponse>;
  login(input: { username: string; password: string }): Promise<void>;
  logout(): Promise<void>;
}

export interface CreateAuthApiOptions {
  readonly baseUrl: string;
  readonly fetch: typeof fetch;
}

function endpoint(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/+$/, "")}${path}`;
}

async function requireSuccess(response: Response): Promise<void> {
  if (!response.ok) {
    throw new Error("Authentication request failed");
  }
}

export function createAuthApi({ baseUrl, fetch: fetchImplementation }: CreateAuthApiOptions): AuthApi {
  return {
    async session() {
      const response = await fetchImplementation(endpoint(baseUrl, "/api/v1/auth/session"), {
        credentials: "same-origin",
      });
      await requireSuccess(response);
      return response.json() as Promise<SessionResponse>;
    },
    async login(input) {
      const response = await fetchImplementation(endpoint(baseUrl, "/api/v1/auth/login"), {
        body: JSON.stringify(input),
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
      await requireSuccess(response);
    },
    async logout() {
      const response = await fetchImplementation(endpoint(baseUrl, "/api/v1/auth/logout"), {
        credentials: "same-origin",
        method: "POST",
      });
      await requireSuccess(response);
    },
  };
}
