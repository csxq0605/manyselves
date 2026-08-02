import { beforeEach, describe, expect, it, vi } from "vitest";

import { getOrCreateBrowserClientId } from "./client-config";

describe("browser client configuration", () => {
  beforeEach(() => {
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
});
