import { describe, expect, it } from "vitest";

import { resolveWebDeployment } from "./deployment-path";

describe("web deployment path", () => {
  it("uses the public subpath for routing and same-origin API calls behind the prefix-stripping proxy", () => {
    expect(resolveWebDeployment({
      assetBaseUrl: "/manyselves/",
      origin: "https://ai.yuanxiaitech.com",
      pathname: "/manyselves/projects/default",
    })).toEqual({
      routerBasename: "/manyselves",
      serverUrl: "https://ai.yuanxiaitech.com/manyselves",
    });
  });

  it("keeps direct port access on root routes and root API calls", () => {
    expect(resolveWebDeployment({
      assetBaseUrl: "/manyselves/",
      origin: "http://127.0.0.1:9090",
      pathname: "/projects/default",
    })).toEqual({
      routerBasename: "/",
      serverUrl: "http://127.0.0.1:9090",
    });
  });

  it("does not confuse a similarly named path with the configured mount path", () => {
    expect(resolveWebDeployment({
      assetBaseUrl: "/manyselves/",
      origin: "https://example.com",
      pathname: "/manyselves-preview",
    })).toEqual({
      routerBasename: "/",
      serverUrl: "https://example.com",
    });
  });
});
