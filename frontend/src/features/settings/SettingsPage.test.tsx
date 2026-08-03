import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SettingsApi } from "./settings-api";
import type { SettingsStorage } from "./settings-storage";
import { SettingsPage } from "./SettingsPage";

const settings = {
  defaults: { maxTokens: 4096, model: "gpt-4.1", provider: "openai", temperature: 0.2 },
  providers: [{ active: true, apiBase: "https://api.openai.com/v1", configured: true, defaultModel: "gpt-4.1", enabled: true, id: "openai-primary", name: "OpenAI Primary", provider: "openai" }],
};

function createApi(): SettingsApi {
  return {
    createProvider: vi.fn().mockResolvedValue(settings), getAgentDebug: vi.fn().mockResolvedValue({ agentId: "main", enabled: false, entries: [] }), getSettings: vi.fn().mockResolvedValue(settings), listAgents: vi.fn().mockResolvedValue({ agents: [{ id: "main", sessionId: "s1", status: "idle" }] }), listPresets: vi.fn().mockResolvedValue({ presets: [] }), removeProvider: vi.fn().mockResolvedValue(settings), syncPresets: vi.fn().mockResolvedValue({ downloaded: 2 }), updateAgentDebug: vi.fn().mockResolvedValue({ agentId: "main", enabled: true, entries: [] }), updateDefaults: vi.fn().mockResolvedValue(settings), updateProvider: vi.fn().mockResolvedValue(settings), validateSettings: vi.fn().mockResolvedValue({ availableProviders: ["openai"], errors: [], valid: true }),
  };
}

const storage: SettingsStorage = {
  loadConnection: () => ({ serverUrl: "https://agents.example" }),
  loadPreferences: () => ({ density: "comfortable", fontSize: 15, notifications: true, previewDefault: "auto", theme: "system" }),
  saveConnection: vi.fn(),
  savePreferences: vi.fn(),
};

describe("SettingsPage", () => {
  it("saves client preferences without an access token field", async () => {
    const user = userEvent.setup();
    render(<SettingsPage api={createApi()} storage={storage} />);
    await screen.findByRole("heading", { name: "Provider 管理" });

    await user.selectOptions(screen.getByLabelText("界面密度"), "compact");
    await user.click(screen.getByRole("button", { name: "保存客户端偏好" }));

    await waitFor(() => expect(storage.savePreferences).toHaveBeenCalledWith(expect.objectContaining({ density: "compact" })));
  });
});
