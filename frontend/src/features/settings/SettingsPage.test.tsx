import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SettingsApi, SettingsResponse } from "./settings-api";
import type { SettingsStorage } from "./settings-storage";
import { SettingsPage } from "./SettingsPage";

const settings: SettingsResponse = {
  defaults: { maxTokens: 8192, model: "mimo-v2.5-pro", provider: "anthropic", temperature: 0.1 },
  providers: [{
    active: true,
    apiBase: "https://token-plan-cn.xiaomimimo.com/anthropic",
    configured: true,
    credentialSource: "yaml",
    defaultModel: "mimo-v2.5-pro",
    enabled: true,
    id: "mimo-cn",
    name: "Xiaomi MiMo Token Plan (China)",
    presetId: "anthropic-xiaomi-mimo-token-plan-china",
    provider: "anthropic",
  }],
};

const presets = [{
  baseUrl: "https://token-plan-cn.xiaomimimo.com/anthropic",
  category: "cn_official",
  defaultModel: "mimo-v2.5-pro",
  description: "MiMo Token Plan",
  id: "anthropic-xiaomi-mimo-token-plan-china",
  name: "Xiaomi MiMo Token Plan (China)",
  provider: "anthropic",
  websiteUrl: "https://xiaomimimo.com",
}];

function createApi(value: SettingsResponse = settings): SettingsApi {
  return {
    createProvider: vi.fn().mockResolvedValue(value),
    getAgentDebug: vi.fn().mockResolvedValue({ agentId: "main", enabled: false, entries: [] }),
    getSettings: vi.fn().mockResolvedValue(value),
    listAgents: vi.fn().mockResolvedValue({ agents: [{ id: "main", sessionId: "s1", status: "idle" }] }),
    listPresets: vi.fn().mockResolvedValue({ presets }),
    removeProvider: vi.fn().mockResolvedValue(value),
    syncPresets: vi.fn().mockResolvedValue({ downloaded: 2 }),
    testProviderConfiguration: vi.fn().mockResolvedValue({ message: "Connection succeeded", model: "mimo-v2.5-pro", ok: true, providerId: null }),
    testProviderConnection: vi.fn().mockResolvedValue({ message: "Connection succeeded", model: "mimo-v2.5-pro", ok: true, providerId: "mimo-cn" }),
    updateAgentDebug: vi.fn().mockResolvedValue({ agentId: "main", enabled: true, entries: [] }),
    updateDefaults: vi.fn().mockResolvedValue(value),
    updateProvider: vi.fn().mockResolvedValue(value),
    upsertProviderConfiguration: vi.fn().mockResolvedValue(value),
    validateSettings: vi.fn().mockResolvedValue({ availableProviders: ["anthropic"], errors: [], valid: true }),
  };
}

const storage: SettingsStorage = {
  loadConnection: () => ({ serverUrl: "https://agents.example" }),
  loadPreferences: () => ({ density: "comfortable", fontSize: 15, notifications: true, previewDefault: "auto", theme: "system" }),
  saveConnection: vi.fn(),
  savePreferences: vi.fn(),
};

describe("SettingsPage", () => {
  it("shows the current application version in the settings header", async () => {
    render(<SettingsPage api={createApi()} storage={storage} />);

    expect(await screen.findByText("版本 V0.0.1")).toBeVisible();
  });

  it("renders the complete provider configuration form", async () => {
    render(<SettingsPage api={createApi()} storage={storage} />);

    expect(await screen.findByLabelText("预设")).toBeVisible();
    expect(screen.getByLabelText("配置名称")).toBeVisible();
    expect(screen.getByLabelText("协议")).toBeVisible();
    expect(screen.getByLabelText("API 地址")).toBeVisible();
    expect(screen.getByLabelText("API Key")).toBeVisible();
    expect(screen.getByLabelText("默认模型")).toBeVisible();
    expect(screen.getByLabelText("设为活动配置")).toBeChecked();
    expect(screen.getByRole("button", { name: "测试连接" })).toBeVisible();
    expect(screen.getByRole("button", { name: "保存配置" })).toBeVisible();
  });

  it("saves client preferences without exposing any credential fields", async () => {
    const user = userEvent.setup();
    render(<SettingsPage api={createApi()} storage={storage} />);

    await screen.findByLabelText("预设");
    await user.click(screen.getByRole("button", { name: "高级设置" }));
    await user.selectOptions(screen.getByLabelText("界面密度"), "compact");
    await user.click(screen.getByRole("button", { name: "保存客户端偏好" }));

    await waitFor(() => expect(storage.savePreferences).toHaveBeenCalledWith(
      expect.objectContaining({ density: "compact" }),
    ));
  });
});
