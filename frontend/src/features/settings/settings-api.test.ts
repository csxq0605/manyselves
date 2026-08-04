import { describe, expect, it, vi } from "vitest";

import type { ApiGateway } from "../../api/gateway";
import { createSettingsApi } from "./settings-api";

describe("settings api", () => {
  it("keeps every server mutation behind the control lease", async () => {
    const requestJson = vi.fn().mockResolvedValue({ providers: [], defaults: {} });
    const gateway = { requestJson } as unknown as ApiGateway;
    const api = createSettingsApi(gateway);

    await api.updateDefaults({ model: "gpt-5.2" });
    await api.updateProvider("provider/one", { apiKey: "one-use-secret" });
    await api.createProvider({ enabled: true, name: "OpenAI", provider: "openai" });
    await api.removeProvider("provider/one");
    await api.syncPresets();
    await api.updateAgentDebug("agent/main", true);

    expect(requestJson.mock.calls.map(([path, init]) => [
      path,
      init?.method,
      init?.requireLease,
    ])).toEqual([
      ["/api/v1/settings", "PATCH", true],
      ["/api/v1/settings/providers/provider%2Fone", "PATCH", true],
      ["/api/v1/settings/providers", "POST", true],
      ["/api/v1/settings/providers/provider%2Fone", "DELETE", true],
      ["/api/v1/settings/presets/sync", "POST", true],
      ["/api/v1/agents/agent%2Fmain/debug", "PATCH", true],
    ]);
    expect(requestJson.mock.calls[1]?.[1]?.json).toEqual({ apiKey: "one-use-secret" });
  });

  it("uses read-only endpoints without requesting a control lease", async () => {
    const requestJson = vi.fn().mockResolvedValue({});
    const api = createSettingsApi({ requestJson } as unknown as ApiGateway);

    await api.getSettings();
    await api.listPresets();
    await api.validateSettings();
    await api.listAgents();
    await api.getAgentDebug("main");
    await api.testProviderConnection("provider/one");

    expect(requestJson.mock.calls.map(([path, init]) => [path, init?.requireLease])).toEqual([
      ["/api/v1/settings", undefined],
      ["/api/v1/settings/presets", undefined],
      ["/api/v1/settings/validate", undefined],
      ["/api/v1/agents", undefined],
      ["/api/v1/agents/main/debug", undefined],
      ["/api/v1/settings/providers/provider%2Fone/test", undefined],
    ]);
  });
});
