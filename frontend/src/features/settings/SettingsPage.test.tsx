import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SettingsApi } from "./settings-api";
import type { SettingsStorage } from "./settings-storage";
import { SettingsPage } from "./SettingsPage";

const settings = {
  defaults: { maxTokens: 4096, model: "gpt-4.1", provider: "openai", temperature: 0.2 },
  providers: [{
    active: true,
    apiBase: "https://api.openai.com/v1",
    configured: true,
    defaultModel: "gpt-4.1",
    enabled: true,
    id: "openai-primary",
    name: "OpenAI Primary",
    provider: "openai",
  }],
};

function createApi(): SettingsApi {
  return {
    createProvider: vi.fn().mockResolvedValue(settings),
    getAgentDebug: vi.fn().mockResolvedValue({ agentId: "main", enabled: false, entries: [] }),
    getSettings: vi.fn().mockResolvedValue(settings),
    listAgents: vi.fn().mockResolvedValue({ agents: [{ id: "main", sessionId: "s1", status: "idle" }] }),
    listPresets: vi.fn().mockResolvedValue({ presets: [{
      baseUrl: "https://api.openai.com/v1", category: "cloud", defaultModel: "gpt-4.1",
      description: "OpenAI", name: "OpenAI", provider: "openai", websiteUrl: "https://openai.com",
    }] }),
    removeProvider: vi.fn().mockResolvedValue(settings),
    syncPresets: vi.fn().mockResolvedValue({ downloaded: 2 }),
    updateAgentDebug: vi.fn().mockResolvedValue({ agentId: "main", enabled: true, entries: [] }),
    updateDefaults: vi.fn().mockResolvedValue(settings),
    updateProvider: vi.fn().mockResolvedValue(settings),
    validateSettings: vi.fn().mockResolvedValue({ availableProviders: ["openai"], errors: [], valid: true }),
  };
}

function createStorage(): SettingsStorage {
  let preferences = {
    density: "comfortable" as const,
    fontSize: 15,
    notifications: true,
    previewDefault: "auto" as const,
    theme: "system" as const,
  };
  let connection = { serverUrl: "https://agents.example", token: "" };
  return {
    loadConnection: () => connection,
    loadPreferences: () => preferences,
    saveConnection: vi.fn((value) => { connection = value; }),
    savePreferences: vi.fn((value) => { preferences = value; }),
  };
}

describe("SettingsPage", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("loads masked provider state without rendering or storing a secret", async () => {
    const api = createApi();
    render(<SettingsPage api={api} storage={createStorage()} />);

    expect(await screen.findByRole("heading", { name: "Provider 管理" })).toBeVisible();
    expect(screen.getAllByText("OpenAI Primary")).not.toHaveLength(0);
    expect(screen.getByText("凭据已配置")).toBeVisible();
    expect(screen.queryByDisplayValue(/sk-/i)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("sk-");
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain("sk-");
  });

  it("confirms an agent restart before replacing a provider credential and clears the input", async () => {
    const user = userEvent.setup();
    const api = createApi();
    render(<SettingsPage api={api} storage={createStorage()} />);
    await screen.findByRole("heading", { name: "Provider 管理" });

    const secretInput = screen.getByLabelText("新凭据");
    await user.type(secretInput, "one-use-secret");
    await user.click(screen.getByRole("button", { name: "替换凭据" }));

    const dialog = screen.getByRole("dialog", { name: "确认重启 Agent" });
    expect(dialog).toHaveTextContent("重新启动 Agent");
    expect(api.updateProvider).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "确认并应用" }));

    await waitFor(() => expect(api.updateProvider).toHaveBeenCalledWith(
      "openai-primary",
      { apiKey: "one-use-secret" },
    ));
    expect(secretInput).toHaveValue("");
    expect(document.body.textContent).not.toContain("one-use-secret");
  });

  it("updates a model without showing the provider restart confirmation", async () => {
    const user = userEvent.setup();
    const api = createApi();
    render(<SettingsPage api={api} storage={createStorage()} />);
    await screen.findByRole("heading", { name: "Provider 管理" });

    await user.clear(screen.getByLabelText("默认模型"));
    await user.type(screen.getByLabelText("默认模型"), "gpt-5.2");
    await user.click(screen.getByRole("button", { name: "保存模型" }));

    await waitFor(() => expect(api.updateDefaults).toHaveBeenCalledWith({ model: "gpt-5.2" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("synchronizes presets, validates settings, and controls per-agent debug mode", async () => {
    const user = userEvent.setup();
    const api = createApi();
    render(<SettingsPage api={api} storage={createStorage()} />);
    await screen.findByRole("heading", { name: "Provider 管理" });

    await user.click(screen.getByRole("button", { name: "同步预设" }));
    expect(await screen.findByText("已同步 2 个预设")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重新校验" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("设置有效"));
    await user.click(screen.getByRole("checkbox", { name: "main 调试模式" }));
    await waitFor(() => expect(api.updateAgentDebug).toHaveBeenCalledWith("main", true));
  });

  it("saves local preferences separately from a session-scoped connection", async () => {
    const user = userEvent.setup();
    const storage = createStorage();
    render(<SettingsPage api={createApi()} storage={storage} onReconnect={vi.fn()} />);
    await screen.findByRole("heading", { name: "Provider 管理" });

    await user.selectOptions(screen.getByLabelText("界面密度"), "compact");
    await user.click(screen.getByRole("button", { name: "保存客户端偏好" }));
    expect(storage.savePreferences).toHaveBeenCalledWith(expect.objectContaining({ density: "compact" }));

    await user.clear(screen.getByLabelText("服务器地址"));
    await user.type(screen.getByLabelText("服务器地址"), "https://new.example/");
    await user.type(screen.getByLabelText("访问令牌"), "session-token");
    await user.click(screen.getByRole("button", { name: "保存并重新连接" }));
    expect(storage.saveConnection).toHaveBeenCalledWith({
      serverUrl: "https://new.example/",
      token: "session-token",
    });
  });
});
