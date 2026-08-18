import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SettingsResponse } from "./settings-api";
import { ProviderConfigurationForm } from "./ProviderConfigurationForm";

const claudePreset = {
  baseUrl: "https://api.anthropic.com",
  category: "official",
  defaultModel: "claude-sonnet-4-20250514",
  description: "Claude official",
  id: "anthropic-claude-official",
  name: "Claude Official",
  provider: "anthropic",
  websiteUrl: "https://www.anthropic.com",
};

const mimoPreset = {
  baseUrl: "https://token-plan-cn.xiaomimimo.com/anthropic",
  category: "cn_official",
  defaultModel: "mimo-v2.5-pro",
  description: "MiMo Token Plan",
  id: "anthropic-xiaomi-mimo-token-plan-china",
  name: "Xiaomi MiMo Token Plan (China)",
  provider: "anthropic",
  websiteUrl: "https://xiaomimimo.com",
};

const settings: SettingsResponse = {
  defaults: { maxTokens: 8192, model: "claude-sonnet-4-20250514", provider: "anthropic", temperature: 0.1 },
  providers: [{
    active: true,
    apiBase: "https://api.anthropic.com",
    configured: true,
    credentialSource: "yaml",
    defaultModel: "claude-sonnet-4-20250514",
    enabled: true,
    id: "claude-official",
    name: "Claude Official",
    presetId: "anthropic-claude-official",
    provider: "anthropic",
  }],
};

function createApi() {
  return {
    testProviderConnection: vi.fn().mockResolvedValue({
      message: "Connection succeeded",
      model: "mimo-v2.5-pro",
      ok: true,
      providerId: "claude-official",
    }),
    testProviderConfiguration: vi.fn().mockResolvedValue({
      message: "Connection succeeded",
      model: "mimo-v2.5-pro",
      ok: true,
      providerId: null,
    }),
    upsertProviderConfiguration: vi.fn().mockResolvedValue(settings),
  };
}

describe("ProviderConfigurationForm", () => {
  it("shows a masked placeholder for a saved API key without filling the input value", () => {
    render(
      <ProviderConfigurationForm
        api={createApi()}
        presets={[claudePreset]}
        settings={settings}
        onSaved={vi.fn()}
      />,
    );

    const apiKeyInput = screen.getByLabelText("API Key");
    expect(apiKeyInput).toHaveAttribute("placeholder", "********（已保存，留空不修改）");
    expect(apiKeyInput).toHaveValue("");
  });

  it("keeps Anthropic-compatible presets distinct and saves the selected preset identity", async () => {
    const user = userEvent.setup();
    const api = createApi();
    render(
      <ProviderConfigurationForm
        api={api}
        presets={[claudePreset, mimoPreset]}
        settings={settings}
        onSaved={vi.fn()}
      />,
    );

    await user.selectOptions(screen.getByLabelText("预设"), mimoPreset.id);
    await user.type(screen.getByLabelText("API Key"), "new-key");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(api.upsertProviderConfiguration).toHaveBeenCalledWith(
      `preset-${mimoPreset.id}`,
      expect.objectContaining({
        apiBase: mimoPreset.baseUrl,
        apiKey: "new-key",
        defaultModel: mimoPreset.defaultModel,
        makeActive: true,
        presetId: mimoPreset.id,
        protocol: "anthropic",
      }),
    ));
  });

  it("tests unsaved values without saving or refreshing settings", async () => {
    const user = userEvent.setup();
    const api = createApi();
    render(
      <ProviderConfigurationForm
        api={api}
        presets={[mimoPreset]}
        settings={settings}
        onSaved={vi.fn()}
      />,
    );

    await user.selectOptions(screen.getByLabelText("预设"), mimoPreset.id);
    await user.type(screen.getByLabelText("API Key"), "probe-key");
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => expect(api.testProviderConfiguration).toHaveBeenCalledWith(
      expect.objectContaining({
        apiKey: "probe-key",
        apiBase: mimoPreset.baseUrl,
        defaultModel: mimoPreset.defaultModel,
        protocol: "anthropic",
      }),
    ));
    expect(api.upsertProviderConfiguration).not.toHaveBeenCalled();
  });

  it("tests a saved provider without requiring API key re-entry", async () => {
    const user = userEvent.setup();
    const api = createApi();
    render(
      <ProviderConfigurationForm
        api={api}
        presets={[claudePreset]}
        settings={settings}
        onSaved={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => expect(api.testProviderConnection).toHaveBeenCalledWith("claude-official"));
    expect(api.testProviderConfiguration).not.toHaveBeenCalled();
  });

  it("keeps an active provider editable when its preset is unavailable offline", () => {
    const savedProvider = settings.providers[0]!;
    const offlineSettings: SettingsResponse = {
      ...settings,
      providers: [{
        ...savedProvider,
        apiBase: mimoPreset.baseUrl,
        defaultModel: mimoPreset.defaultModel,
        id: "mimo-cn",
        name: mimoPreset.name,
        presetId: mimoPreset.id,
      }],
    };

    render(
      <ProviderConfigurationForm
        api={createApi()}
        presets={[claudePreset]}
        settings={offlineSettings}
        onSaved={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("combobox")[0]).toHaveValue("__custom__");
    expect(screen.getByDisplayValue(mimoPreset.name)).toBeVisible();
    expect(screen.getByDisplayValue(mimoPreset.baseUrl)).toBeVisible();
    expect(screen.getByDisplayValue(mimoPreset.defaultModel)).toBeVisible();
  });
});
