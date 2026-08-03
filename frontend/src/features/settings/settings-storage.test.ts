import { beforeEach, describe, expect, it } from "vitest";

import {
  createBrowserSettingsStorage,
  defaultClientPreferences,
  resolveServerUrl,
} from "./settings-storage";

describe("browser settings storage", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    document.documentElement.removeAttribute("data-density");
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.style.removeProperty("--manyselves-font-size");
  });

  it("persists versioned UI preferences locally and applies them to the document", () => {
    const storage = createBrowserSettingsStorage({
      localStorage,
      root: document.documentElement,
      sessionStorage,
    });
    const preferences = {
      ...defaultClientPreferences,
      density: "compact" as const,
      fontSize: 17,
      theme: "dark" as const,
    };

    storage.savePreferences(preferences);

    expect(storage.loadPreferences()).toEqual(preferences);
    expect(localStorage.getItem("manyselves.preferences.v1")).toBe(JSON.stringify({
      version: 1,
      value: preferences,
    }));
    expect(document.documentElement.dataset.density).toBe("compact");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.style.getPropertyValue("--manyselves-font-size")).toBe("17px");
  });

  it("keeps deployment tokens session-scoped and never copies them into local storage", () => {
    const storage = createBrowserSettingsStorage({
      localStorage,
      root: document.documentElement,
      sessionStorage,
    });

    storage.saveConnection({ serverUrl: "https://agents.example/", token: "deploy-secret" });

    expect(storage.loadConnection()).toEqual({
      serverUrl: "https://agents.example",
      token: "deploy-secret",
    });
    expect(sessionStorage.getItem("manyselves.deploymentToken")).toBe("deploy-secret");
    expect(localStorage.getItem("manyselves.serverUrl.v1")).toBe("https://agents.example");
    expect(JSON.stringify({ ...localStorage })).not.toContain("deploy-secret");
  });

  it("falls back safely when persisted preferences are malformed", () => {
    localStorage.setItem("manyselves.preferences.v1", "not-json");
    const storage = createBrowserSettingsStorage({
      localStorage,
      root: document.documentElement,
      sessionStorage,
    });

    expect(storage.loadPreferences()).toEqual(defaultClientPreferences);
  });

  it("boots from an explicit deployment URL, then a saved URL, then the page origin", () => {
    expect(resolveServerUrl({
      environmentUrl: " https://deploy.example/ ",
      origin: "https://page.example",
      savedUrl: "https://saved.example",
    })).toBe("https://deploy.example");
    expect(resolveServerUrl({
      environmentUrl: "",
      origin: "https://page.example",
      savedUrl: "https://saved.example/",
    })).toBe("https://saved.example");
    expect(resolveServerUrl({
      origin: "https://page.example/",
      savedUrl: "",
    })).toBe("https://page.example");
  });
});
