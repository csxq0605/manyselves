import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SettingsApi, SettingsResponse } from "./settings-api";
import type { SettingsStorage } from "./settings-storage";
import { SettingsPage } from "./SettingsPage";

const settings: SettingsResponse = {
  defaults: { maxTokens: 4096, model: "gpt-4.1", provider: "openai", temperature: 0.2 },
  providers: [{ active: true, apiBase: "https://api.openai.com/v1", configured: true, credentialSource: "yaml", defaultModel: "gpt-4.1", enabled: true, id: "openai-primary", name: "OpenAI Primary", provider: "openai" }],
};

function createApi(value: SettingsResponse = settings): SettingsApi {
  return {
    createProvider: vi.fn().mockResolvedValue(value), getAgentDebug: vi.fn().mockResolvedValue({ agentId: "main", enabled: false, entries: [] }), getSettings: vi.fn().mockResolvedValue(value), listAgents: vi.fn().mockResolvedValue({ agents: [{ id: "main", sessionId: "s1", status: "idle" }] }), listPresets: vi.fn().mockResolvedValue({ presets: [] }), removeProvider: vi.fn().mockResolvedValue(value), syncPresets: vi.fn().mockResolvedValue({ downloaded: 2 }), testProviderConnection: vi.fn().mockResolvedValue({ message: "Connection succeeded", model: "gpt-4.1", ok: true, providerId: "openai-primary" }), updateAgentDebug: vi.fn().mockResolvedValue({ agentId: "main", enabled: true, entries: [] }), updateDefaults: vi.fn().mockResolvedValue(value), updateProvider: vi.fn().mockResolvedValue(value), validateSettings: vi.fn().mockResolvedValue({ availableProviders: ["openai"], errors: [], valid: true }),
  };
}

const storage: SettingsStorage = {
  loadConnection: () => ({ serverUrl: "https://agents.example" }),
  loadPreferences: () => ({ density: "comfortable", fontSize: 15, notifications: true, previewDefault: "auto", theme: "system" }),
  saveConnection: vi.fn(),
  savePreferences: vi.fn(),
};

describe("SettingsPage", () => {
  it("renders the approved simple model form", async () => {
    render(<SettingsPage api={createApi()} storage={storage} />);

    expect(await screen.findByLabelText("模型提供商")).toBeVisible();
    expect(screen.getByLabelText("API 地址（可选）")).toBeVisible();
    expect(screen.getByLabelText("API Key")).toBeVisible();
    expect(screen.getByLabelText("默认模型")).toBeVisible();
    expect(screen.getByRole("button", { name: "测试连接" })).toBeVisible();
    expect(screen.getByRole("button", { name: "保存" })).toBeVisible();
    expect(screen.getByText("高级设置")).toBeVisible();
    expect(screen.queryByText("RUNTIME CONTROL PLANE")).not.toBeInTheDocument();
  });

  it("locks an environment-managed key without displaying it", async () => {
    const environmentSettings: SettingsResponse = {
      ...settings,
      providers: [{ ...settings.providers[0]!, credentialSource: "environment" }],
    };
    render(<SettingsPage api={createApi(environmentSettings)} storage={storage} />);

    expect(await screen.findByLabelText("API Key")).toBeDisabled();
    expect(screen.getByText("由服务器环境管理")).toBeVisible();
    expect(screen.queryByDisplayValue(/secret/i)).not.toBeInTheDocument();
  });

  it("tests the saved connection and submits a newly typed key only on save", async () => {
    const user = userEvent.setup();
    const api = createApi();
    render(<SettingsPage api={api} storage={storage} />);

    await user.click(await screen.findByRole("button", { name: "测试连接" }));
    await waitFor(() => expect(api.testProviderConnection).toHaveBeenCalledWith("openai-primary"));
    expect(await screen.findByText("连接成功 · gpt-4.1")).toBeVisible();

    await user.type(screen.getByLabelText("API Key"), "new-provider-key");
    await user.clear(screen.getByLabelText("默认模型"));
    await user.type(screen.getByLabelText("默认模型"), "gpt-5");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("dialog", { name: "确认重启 Agent" })).toBeVisible();
    expect(api.updateDefaults).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "确认并应用" }));

    await waitFor(() => expect(api.updateDefaults).toHaveBeenCalledWith({
      activeProviderId: "openai-primary",
      apiBase: "https://api.openai.com/v1",
      apiKey: "new-provider-key",
      model: "gpt-5",
      provider: "openai",
    }));
    expect(api.updateProvider).not.toHaveBeenCalled();
  });

  it("keeps a newly typed key available when saving fails", async () => {
    const user = userEvent.setup();
    const api = createApi();
    vi.mocked(api.updateDefaults).mockRejectedValueOnce(new Error("save failed"));
    render(<SettingsPage api={api} storage={storage} />);

    const keyInput = await screen.findByLabelText("API Key");
    await user.type(keyInput, "retry-this-key");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await user.click(await screen.findByRole("button", { name: "确认并应用" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("save failed");
    expect(keyInput).toHaveValue("retry-this-key");
  });

  it("uses the same restart confirmation for advanced provider changes", async () => {
    const user = userEvent.setup();
    const api = createApi();
    render(<SettingsPage api={api} storage={storage} />);

    await screen.findByLabelText("模型提供商");
    await user.click(screen.getByText("高级设置"));
    await user.click(screen.getByRole("button", { name: "停用" }));

    expect(await screen.findByRole("dialog", { name: "确认重启 Agent" })).toHaveTextContent(
      "停用“OpenAI Primary”提供商",
    );
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(api.updateProvider).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "停用" }));
    await user.click(await screen.findByRole("button", { name: "确认并应用" }));
    await waitFor(() => expect(api.updateProvider).toHaveBeenCalledWith("openai-primary", { enabled: false }));
  });

  it("saves client preferences without an access token field", async () => {
    const user = userEvent.setup();
    render(<SettingsPage api={createApi()} storage={storage} />);
    await screen.findByLabelText("模型提供商");
    await user.click(screen.getByText("高级设置"));

    await user.selectOptions(screen.getByLabelText("界面密度"), "compact");
    await user.click(screen.getByRole("button", { name: "保存客户端偏好" }));

    await waitFor(() => expect(storage.savePreferences).toHaveBeenCalledWith(expect.objectContaining({ density: "compact" })));
  });
});
