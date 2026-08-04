import { describe, expect, it, vi } from "vitest";

import { createProjectApi } from "./project-api";

describe("project API metadata operations", () => {
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
