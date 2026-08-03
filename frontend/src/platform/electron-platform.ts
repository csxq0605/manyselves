import type { LocalFileRef, NotificationInput, PlatformBridge, SaveDownloadInput } from "./types";

interface NativeFileRef {
  readonly bytes: Uint8Array;
  readonly name: string;
  readonly relativePath?: string;
  readonly size: number;
  readonly type: string;
}

export interface RendererDesktopApi {
  notify(input: NotificationInput): Promise<void>;
  openDownloadedFile(path: string): Promise<void>;
  saveDownload(input: { readonly bytes: Uint8Array; readonly suggestedName: string }): Promise<string | null>;
  selectDirectory(): Promise<{ readonly files: readonly NativeFileRef[]; readonly name: string; readonly skipped: readonly string[] } | null>;
  selectFiles(): Promise<readonly NativeFileRef[]>;
  secureToken: { delete(): Promise<void>; get(): Promise<string | null>; set(token: string): Promise<void> };
}

function toLocalFile(reference: NativeFileRef): LocalFileRef {
  const file = new File([Uint8Array.from(reference.bytes).buffer], reference.name, { type: reference.type });
  return {
    file,
    name: reference.name,
    ...(reference.relativePath ? { relativePath: reference.relativePath } : {}),
    size: reference.size,
    type: reference.type,
  };
}

export class ElectronPlatformBridge implements PlatformBridge {
  readonly kind = "electron" as const;
  constructor(private readonly api: RendererDesktopApi) {}

  async selectFiles(): Promise<readonly LocalFileRef[]> {
    return (await this.api.selectFiles()).map(toLocalFile);
  }
  async selectDirectory() {
    const selected = await this.api.selectDirectory();
    return selected === null ? null : { files: selected.files.map(toLocalFile), name: selected.name };
  }
  async saveDownload(input: SaveDownloadInput): Promise<void> {
    await this.api.saveDownload({ bytes: new Uint8Array(await input.blob.arrayBuffer()), suggestedName: input.suggestedName });
  }
  openDownloadedFile(path: string): Promise<void> { return this.api.openDownloadedFile(path); }
  notify(input: NotificationInput): Promise<void> { return this.api.notify(input); }
}

declare global {
  interface Window { readonly manyselvesDesktop?: RendererDesktopApi; }
}
