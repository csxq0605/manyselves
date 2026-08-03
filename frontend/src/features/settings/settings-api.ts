import type { ApiGateway } from "../../api/gateway";
import type { components } from "../../api/generated/schema";

export type AgentDebugResponse = components["schemas"]["AgentDebugResponse"];
export type AgentListResponse = components["schemas"]["AgentListResponse"];
export type PresetListResponse = components["schemas"]["PresetListResponse"];
export type PresetSyncResponse = components["schemas"]["PresetSyncResponse"];
export type ProviderSettingsCreate = components["schemas"]["ProviderSettingsCreate"];
export type ProviderSettingsUpdate = components["schemas"]["ProviderSettingsUpdate"];
export type SettingsDefaultsUpdate = components["schemas"]["SettingsDefaultsUpdate"];
export type SettingsResponse = components["schemas"]["SettingsResponse"];
export type SettingsValidationResponse = components["schemas"]["SettingsValidationResponse"];

export interface SettingsApi {
  createProvider(input: ProviderSettingsCreate): Promise<SettingsResponse>;
  getAgentDebug(agentId: string): Promise<AgentDebugResponse>;
  getSettings(): Promise<SettingsResponse>;
  listAgents(): Promise<AgentListResponse>;
  listPresets(): Promise<PresetListResponse>;
  removeProvider(providerId: string): Promise<SettingsResponse>;
  syncPresets(): Promise<PresetSyncResponse>;
  updateAgentDebug(agentId: string, enabled: boolean): Promise<AgentDebugResponse>;
  updateDefaults(input: SettingsDefaultsUpdate): Promise<SettingsResponse>;
  updateProvider(providerId: string, input: ProviderSettingsUpdate): Promise<SettingsResponse>;
  validateSettings(): Promise<SettingsValidationResponse>;
}

export function createSettingsApi(gateway: ApiGateway): SettingsApi {
  return {
    createProvider: (input) => gateway.requestJson("/api/v1/settings/providers", {
      json: input,
      method: "POST",
      requireLease: true,
    }),
    getAgentDebug: (agentId) => gateway.requestJson(
      `/api/v1/agents/${encodeURIComponent(agentId)}/debug`,
    ),
    getSettings: () => gateway.requestJson("/api/v1/settings"),
    listAgents: () => gateway.requestJson("/api/v1/agents"),
    listPresets: () => gateway.requestJson("/api/v1/settings/presets"),
    removeProvider: (providerId) => gateway.requestJson(
      `/api/v1/settings/providers/${encodeURIComponent(providerId)}`,
      { method: "DELETE", requireLease: true },
    ),
    syncPresets: () => gateway.requestJson("/api/v1/settings/presets/sync", {
      method: "POST",
      requireLease: true,
    }),
    updateAgentDebug: (agentId, enabled) => gateway.requestJson(
      `/api/v1/agents/${encodeURIComponent(agentId)}/debug`,
      { json: { enabled }, method: "PATCH", requireLease: true },
    ),
    updateDefaults: (input) => gateway.requestJson("/api/v1/settings", {
      json: input,
      method: "PATCH",
      requireLease: true,
    }),
    updateProvider: (providerId, input) => gateway.requestJson(
      `/api/v1/settings/providers/${encodeURIComponent(providerId)}`,
      { json: input, method: "PATCH", requireLease: true },
    ),
    validateSettings: () => gateway.requestJson("/api/v1/settings/validate", {
      method: "POST",
    }),
  };
}
