import { describe, expect, it, vi } from "vitest";

import { AuthRequestError, createAuthApi } from "./auth-api";

describe("AuthApi", () => {
  it("uses same-origin cookies for the session lifecycle endpoints", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json({ authenticated: true, expiresAt: "2026-08-03T00:00:00Z", username: "admin" }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const api = createAuthApi({ baseUrl: "https://agents.example/", fetch });

    await api.session();
    await api.login({ password: "password", username: "admin" });
    await api.logout();

    expect(fetch.mock.calls).toEqual(expect.arrayContaining([
      ["https://agents.example/api/v1/auth/session", expect.objectContaining({ credentials: "same-origin" })],
      ["https://agents.example/api/v1/auth/login", expect.objectContaining({ body: JSON.stringify({ password: "password", username: "admin" }), credentials: "same-origin", method: "POST" })],
      ["https://agents.example/api/v1/auth/logout", expect.objectContaining({ credentials: "same-origin", method: "POST" })],
    ]));
  });

  it("does not expose an authentication response body on failure", async () => {
    const api = createAuthApi({
      baseUrl: "",
      fetch: vi.fn<typeof globalThis.fetch>().mockResolvedValue(new Response("credential diagnostic", { status: 500 })),
    });

    await expect(api.session()).rejects.toEqual(expect.objectContaining({
      message: "Authentication request failed",
      status: 500,
    }));
    await expect(api.session()).rejects.toBeInstanceOf(AuthRequestError);
  });
});
