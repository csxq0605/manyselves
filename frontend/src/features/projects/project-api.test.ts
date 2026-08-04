import { describe, expect, it, vi } from "vitest";

import { createApiGateway } from "../../api/gateway";
import { createProjectApi } from "./project-api";

describe("project API metadata operations", () => {
  it("acquires a browser control lease before a real project mutation", async () => {
    let leaseToken: string | null = null;
    const fetch = vi.fn<typeof window.fetch>()
      .mockResolvedValueOnce(Response.json({ actorId: "admin", clientId: "browser", expiresAt: "2030-01-01T00:01:00Z", leaseToken: "lease-1" }, { status: 201 }))
      .mockResolvedValueOnce(Response.json({ active: false, description: "", displayName: "Planning", id: "planning", revision: "r1" }));
    const api = createProjectApi(createApiGateway({ baseUrl: "https://server", clientId: "browser", fetch, getLeaseToken: () => leaseToken, setLeaseToken: (token) => { leaseToken = token; } }));

    await api.create({ description: "", displayName: "Planning", projectId: "planning" });

    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "https://server/api/v1/control/lease",
      "https://server/api/v1/projects",
    ]);
    expect(new Headers(fetch.mock.calls[1]?.[1]?.headers).get("X-Control-Lease-Token")).toBe("lease-1");
  });

  it("sends display metadata when creating and updating a stable project", async () => {
    const requestJson = vi.fn().mockResolvedValue({
      active: false,
      description: "Planning workspace",
      displayName: "Planning",
      id: "planning-2026",
      revision: "a".repeat(64),
    });
    const requestVoid = vi.fn().mockResolvedValue(undefined);
    const api = createProjectApi({ requestJson, requestVoid } as never);

    await api.create({ description: "Planning workspace", displayName: "Planning", projectId: "planning-2026" });
    await api.update("planning-2026", {
      description: "Updated planning workspace",
      displayName: "Planning revised",
      revision: "a".repeat(64),
    });
    await api.delete("planning-2026");

    expect(requestJson).toHaveBeenNthCalledWith(1, "/api/v1/projects", {
      json: { description: "Planning workspace", displayName: "Planning", projectId: "planning-2026" },
      method: "POST",
      requireLease: true,
    });
    expect(requestJson).toHaveBeenNthCalledWith(2, "/api/v1/projects/planning-2026", {
      json: { description: "Updated planning workspace", displayName: "Planning revised", revision: "a".repeat(64) },
      method: "PATCH",
      requireLease: true,
    });
    expect(requestVoid).toHaveBeenCalledWith("/api/v1/projects/planning-2026", {
      method: "DELETE",
      requireLease: true,
    });
  });
});
