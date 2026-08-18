import { describe, expect, it, vi } from "vitest";

vi.mock("electron", () => ({ shell: { openExternal: vi.fn() } }));

import { createWindowOptions, isTrustedRendererUrl } from "./security.js";

describe("desktop security", () => {
  it("creates every application window with hardened preferences", () => {
    expect(createWindowOptions("preload.js").webPreferences).toMatchObject({
      contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true,
    });
  });

  it("trusts only the packaged file or configured local development origin", () => {
    expect(isTrustedRendererUrl(
      "file:///opt/Manyselves/resources/frontend/index.html",
      "file:///opt/Manyselves/resources/frontend/index.html",
    )).toBe(true);
    expect(isTrustedRendererUrl(
      "file:///tmp/untrusted/index.html",
      "file:///opt/Manyselves/resources/frontend/index.html",
    )).toBe(false);
    expect(isTrustedRendererUrl("https://evil.example/index.html")).toBe(false);
    expect(isTrustedRendererUrl("http://127.0.0.1:4173/", "http://127.0.0.1:4173")).toBe(true);
  });
});
