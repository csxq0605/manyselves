import { beforeEach, describe, expect, it, vi } from "vitest";

import { getOrCreateBrowserClientId } from "./client-config";

describe("browser client configuration", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("reuses one versioned client id across browser reloads", () => {
    const randomUuid = vi.fn(() => "client-generated");

    const first = getOrCreateBrowserClientId(window.localStorage, randomUuid);
    const second = getOrCreateBrowserClientId(window.localStorage, randomUuid);

    expect(first).toBe("client-generated");
    expect(second).toBe("client-generated");
    expect(randomUuid).toHaveBeenCalledOnce();
    expect(window.localStorage.getItem("manyselves.clientId.v1")).toBe("client-generated");
  });

  it("creates a client id when insecure HTTP omits crypto.randomUUID", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto),
    });

    const clientId = getOrCreateBrowserClientId(window.localStorage);

    expect(clientId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });
});
