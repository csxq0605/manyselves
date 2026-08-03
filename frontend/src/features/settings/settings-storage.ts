export type ClientDensity = "comfortable" | "compact";
export type ClientTheme = "system" | "light" | "dark";
export type PreviewDefault = "auto" | "source" | "preview";

export interface ClientPreferences {
  readonly density: ClientDensity;
  readonly fontSize: number;
  readonly notifications: boolean;
  readonly previewDefault: PreviewDefault;
  readonly theme: ClientTheme;
}

export interface ServerConnection {
  readonly serverUrl: string;
  readonly token: string;
}

export interface SettingsStorage {
  readonly tokenScope?: "secure-device" | "session";
  loadConnection(): ServerConnection;
  loadPreferences(): ClientPreferences;
  saveConnection(value: ServerConnection): void;
  savePreferences(value: ClientPreferences): void;
}

export interface SecureTokenAdapter {
  delete(): Promise<void>;
  get(): Promise<string | null>;
  set(token: string): Promise<void>;
}

export interface BrowserSettingsStorageOptions {
  readonly defaultServerUrl?: string;
  readonly localStorage: Pick<Storage, "getItem" | "removeItem" | "setItem">;
  readonly root: HTMLElement;
  readonly sessionStorage: Pick<Storage, "getItem" | "removeItem" | "setItem">;
}

export const defaultClientPreferences: ClientPreferences = {
  density: "comfortable",
  fontSize: 15,
  notifications: true,
  previewDefault: "auto",
  theme: "system",
};

const preferenceKey = "manyselves.preferences.v1";
const serverUrlKey = "manyselves.serverUrl.v1";
const tokenKey = "manyselves.deploymentToken";

function isPreferences(value: unknown): value is ClientPreferences {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const input = value as Partial<ClientPreferences>;
  return (
    (input.density === "comfortable" || input.density === "compact")
    && Number.isInteger(input.fontSize)
    && (input.fontSize ?? 0) >= 12
    && (input.fontSize ?? 0) <= 22
    && typeof input.notifications === "boolean"
    && ["auto", "source", "preview"].includes(input.previewDefault ?? "")
    && ["system", "light", "dark"].includes(input.theme ?? "")
  );
}

function normalizeServerUrl(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

export interface ResolveServerUrlInput {
  readonly environmentUrl?: string;
  readonly origin: string;
  readonly savedUrl: string;
}

export function resolveServerUrl(input: ResolveServerUrlInput): string {
  return normalizeServerUrl(
    input.environmentUrl?.trim() || input.savedUrl.trim() || input.origin,
  );
}

function applyPreferences(root: HTMLElement, value: ClientPreferences): void {
  root.dataset.density = value.density;
  root.dataset.theme = value.theme;
  root.style.setProperty("--manyselves-font-size", `${value.fontSize}px`);
}

export function createBrowserSettingsStorage(
  options: BrowserSettingsStorageOptions,
): SettingsStorage {
  return {
    tokenScope: "session",
    loadConnection() {
      return {
        serverUrl: options.localStorage.getItem(serverUrlKey) ?? options.defaultServerUrl ?? window.location.origin,
        token: options.sessionStorage.getItem(tokenKey) ?? "",
      };
    },
    loadPreferences() {
      let value = defaultClientPreferences;
      const encoded = options.localStorage.getItem(preferenceKey);
      if (encoded) {
        try {
          const parsed = JSON.parse(encoded) as { value?: unknown; version?: unknown };
          if (parsed.version === 1 && isPreferences(parsed.value)) {
            value = parsed.value;
          }
        } catch {
          value = defaultClientPreferences;
        }
      }
      applyPreferences(options.root, value);
      return value;
    },
    saveConnection(value) {
      const serverUrl = normalizeServerUrl(value.serverUrl);
      if (serverUrl) {
        options.localStorage.setItem(serverUrlKey, serverUrl);
      } else {
        options.localStorage.removeItem(serverUrlKey);
      }
      if (value.token) {
        options.sessionStorage.setItem(tokenKey, value.token);
      } else {
        options.sessionStorage.removeItem(tokenKey);
      }
    },
    savePreferences(value) {
      options.localStorage.setItem(preferenceKey, JSON.stringify({ version: 1, value }));
      applyPreferences(options.root, value);
    },
  };
}

export function createElectronSettingsStorage(
  base: SettingsStorage,
  secureToken: SecureTokenAdapter,
): SettingsStorage {
  return {
    tokenScope: "secure-device",
    loadConnection: () => base.loadConnection(),
    loadPreferences: () => base.loadPreferences(),
    saveConnection(value) {
      base.saveConnection(value);
      void (value.token ? secureToken.set(value.token) : secureToken.delete());
    },
    savePreferences: (value) => base.savePreferences(value),
  };
}
